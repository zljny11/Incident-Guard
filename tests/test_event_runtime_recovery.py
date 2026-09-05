from __future__ import annotations

import asyncio

import pytest

from incident_guard.agents.event_runtime import (
    CrashInjected,
    DurableApprovalProvider,
    EventDrivenAgentRuntime,
)
from incident_guard.agents.provider import (
    ProviderEvent,
    ProviderResponse,
    ProviderUsage,
    StopReason,
    ToolCall,
)
from incident_guard.agents.react_runtime import FakeToolExecutor, RunLimits
from incident_guard.agents.run_models import RunStatus
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)
from incident_guard.agents.tool_pipeline import (
    PolicyAction,
    PolicyDecision,
    RegistryToolExecutor,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)
from incident_guard.events import SQLiteEventStore


def events(response: ProviderResponse):
    result = []
    if response.text:
        result.append(ProviderEvent.text_delta(response.text))
    result.extend(ProviderEvent.tool_call(call) for call in response.tool_calls)
    result.append(ProviderEvent.completed(response))
    return result


@pytest.mark.parametrize("crash_event", ["tool.completed", "step.completed"])
def test_resume_does_not_repeat_completed_tool_side_effect(
    tmp_path, crash_event: str
) -> None:
    store = SQLiteEventStore(tmp_path / f"{crash_event}.db")
    call = ToolCall("health-1", "query_health", {})
    tool_response = ProviderResponse(
        stop_reason=StopReason.TOOL_USE, tool_calls=(call,)
    )
    executor = FakeToolExecutor({"health-1": "unhealthy"})

    crashed = False

    def inject(event) -> None:
        nonlocal crashed
        if event.event_type == crash_event and not crashed:
            crashed = True
            raise CrashInjected(crash_event)

    first = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([events(tool_response)]),
        executor,
        store,
        fault_injector=inject,
    )
    with pytest.raises(CrashInjected):
        asyncio.run(first.run("run-001", [{"role": "user", "content": "alert"}]))

    resumed = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([events(ProviderResponse(text="root cause"))]),
        executor,
        store,
    )
    projection = asyncio.run(resumed.resume("run-001"))

    assert projection.status is RunStatus.COMPLETED
    assert [item.id for item in executor.calls] == ["health-1"]
    assert sum(
        event.event_type == "tool.completed"
        for event in store.replay("run-001")
    ) == 1


def test_started_mutation_becomes_failed_uncertain_and_is_not_retried(
    tmp_path,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    calls = 0

    def mutate(_arguments):
        nonlocal calls
        calls += 1
        return "restarted"

    registry = ToolRegistry(
        (
            ToolDefinition(
                "restart_service",
                {"type": "object", "additionalProperties": False},
                mutate,
                effect=ToolEffect.MUTATE,
            ),
        )
    )
    call = ToolCall("restart-1", "restart_service", {})
    response = ProviderResponse(
        stop_reason=StopReason.TOOL_USE, tool_calls=(call,)
    )

    def inject(event) -> None:
        if event.event_type == "tool.started":
            raise CrashInjected("mutation window")

    first_executor = RegistryToolExecutor(
        registry,
        approval_provider=DurableApprovalProvider(store, "run-001"),
    )
    first = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([events(response)]),
        first_executor,
        store,
    )
    waiting = asyncio.run(
        first.run("run-001", [{"role": "user", "content": "alert"}])
    )
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert calls == 0
    first.decide_approval(
        "run-001", "restart-1", approved=True, reason="operator approved"
    )

    approved_executor = RegistryToolExecutor(
        registry,
        approval_provider=DurableApprovalProvider(store, "run-001"),
    )
    crashing = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([]),
        approved_executor,
        store,
        fault_injector=inject,
    )
    with pytest.raises(CrashInjected):
        asyncio.run(crashing.resume("run-001"))

    resumed_executor = RegistryToolExecutor(
        registry,
        approval_provider=DurableApprovalProvider(store, "run-001"),
    )
    resumed = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([]), resumed_executor, store
    )
    projection = asyncio.run(resumed.resume("run-001"))

    assert projection.status is RunStatus.FAILED_UNCERTAIN
    assert "restart-1" in (projection.failure_reason or "")
    assert calls == 0


def test_mutation_pauses_for_durable_approval_then_resumes_once(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    calls = 0

    def mutate(_arguments):
        nonlocal calls
        calls += 1
        return "restarted"

    registry = ToolRegistry(
        (
            ToolDefinition(
                "restart_service",
                {"type": "object", "additionalProperties": False},
                mutate,
                effect=ToolEffect.MUTATE,
            ),
        )
    )
    call = ToolCall("restart-1", "restart_service", {})
    executor = RegistryToolExecutor(
        registry,
        approval_provider=DurableApprovalProvider(store, "run-approval"),
    )
    runtime = EventDrivenAgentRuntime(
        ScriptedStreamingProvider(
            [
                events(
                    ProviderResponse(
                        stop_reason=StopReason.TOOL_USE,
                        tool_calls=(call,),
                    )
                ),
                events(ProviderResponse(text="recovered")),
            ]
        ),
        executor,
        store,
    )

    waiting = asyncio.run(
        runtime.run("run-approval", [{"role": "user", "content": "alert"}])
    )
    assert waiting.status is RunStatus.WAITING_APPROVAL
    assert calls == 0

    decided = runtime.decide_approval(
        "run-approval",
        "restart-1",
        approved=True,
        reason="operator approved",
    )
    assert decided.status is RunStatus.RUNNING

    completed = asyncio.run(runtime.resume("run-approval"))
    assert completed.status is RunStatus.COMPLETED
    assert calls == 1
    assert [
        event.event_type for event in store.replay("run-approval")
    ].count("approval.requested") == 1


def test_rejected_durable_approval_never_executes_mutation(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    calls = 0

    def mutate(_arguments):
        nonlocal calls
        calls += 1
        return "restarted"

    registry = ToolRegistry(
        (
            ToolDefinition(
                "restart_service",
                {"type": "object", "additionalProperties": False},
                mutate,
                effect=ToolEffect.MUTATE,
            ),
        )
    )
    runtime = EventDrivenAgentRuntime(
        ScriptedStreamingProvider(
            [
                events(
                    ProviderResponse(
                        stop_reason=StopReason.TOOL_USE,
                        tool_calls=(
                            ToolCall("restart-1", "restart_service", {}),
                        ),
                    )
                )
            ]
        ),
        RegistryToolExecutor(registry),
        store,
    )

    waiting = asyncio.run(
        runtime.run("run-rejected", [{"role": "user", "content": "alert"}])
    )
    assert waiting.status is RunStatus.WAITING_APPROVAL
    rejected = runtime.decide_approval(
        "run-rejected",
        "restart-1",
        approved=False,
        reason="operator rejected",
    )

    assert rejected.status is RunStatus.FAILED
    assert calls == 0


def test_policy_denied_mutation_fails_without_requesting_approval(tmp_path) -> None:
    class DenyPolicy:
        def evaluate(self, _call, _definition):
            return PolicyDecision(PolicyAction.DENY, "wrong recovery action")

    store = SQLiteEventStore(tmp_path / "events.db")
    calls = 0

    def mutate(_arguments):
        nonlocal calls
        calls += 1
        return "changed"

    registry = ToolRegistry(
        (
            ToolDefinition(
                "rollback_service",
                {"type": "object", "additionalProperties": False},
                mutate,
                effect=ToolEffect.MUTATE,
            ),
        )
    )
    runtime = EventDrivenAgentRuntime(
        ScriptedStreamingProvider(
            [
                events(
                    ProviderResponse(
                        stop_reason=StopReason.TOOL_USE,
                        tool_calls=(
                            ToolCall("rollback-1", "rollback_service", {}),
                        ),
                    )
                ),
                events(ProviderResponse(text="escalated")),
            ]
        ),
        RegistryToolExecutor(registry, policy=DenyPolicy()),
        store,
    )

    result = asyncio.run(
        runtime.run("run-denied", [{"role": "user", "content": "alert"}])
    )

    assert result.status is RunStatus.COMPLETED
    assert calls == 0
    assert all(
        event.event_type != "approval.requested"
        for event in store.replay("run-denied")
    )


def test_approved_mutation_execution_error_becomes_failed_uncertain(
    tmp_path,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")

    def uncertain(_arguments):
        raise TimeoutError("transport lost after request")

    registry = ToolRegistry(
        (
            ToolDefinition(
                "restart_service",
                {"type": "object", "additionalProperties": False},
                uncertain,
                effect=ToolEffect.MUTATE,
            ),
        )
    )
    runtime = EventDrivenAgentRuntime(
        ScriptedStreamingProvider(
            [
                events(
                    ProviderResponse(
                        stop_reason=StopReason.TOOL_USE,
                        tool_calls=(
                            ToolCall("restart-1", "restart_service", {}),
                        ),
                    )
                )
            ]
        ),
        RegistryToolExecutor(
            registry,
            approval_provider=DurableApprovalProvider(store, "run-uncertain"),
        ),
        store,
    )

    waiting = asyncio.run(
        runtime.run("run-uncertain", [{"role": "user", "content": "alert"}])
    )
    assert waiting.status is RunStatus.WAITING_APPROVAL
    runtime.decide_approval(
        "run-uncertain",
        "restart-1",
        approved=True,
        reason="operator approved",
    )

    result = asyncio.run(runtime.resume("run-uncertain"))
    assert result.status is RunStatus.FAILED_UNCERTAIN


def test_started_read_tool_is_safely_retried_after_resume(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    call = ToolCall("health-1", "query_health", {})
    response = ProviderResponse(
        stop_reason=StopReason.TOOL_USE, tool_calls=(call,)
    )
    executor = FakeToolExecutor({"health-1": "unhealthy"})

    def inject(event) -> None:
        if event.event_type == "tool.started":
            raise CrashInjected("read interrupted")

    first = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([events(response)]),
        executor,
        store,
        fault_injector=inject,
    )
    with pytest.raises(CrashInjected):
        asyncio.run(first.run("run-001", [{"role": "user", "content": "alert"}]))

    resumed = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([events(ProviderResponse(text="done"))]),
        executor,
        store,
    )
    result = asyncio.run(resumed.resume("run-001"))

    assert result.status is RunStatus.COMPLETED
    assert [item.id for item in executor.calls] == ["health-1"]


def test_cancel_is_durable_and_resume_finishes_without_more_model_calls(
    tmp_path,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")

    def inject(event) -> None:
        if event.event_type == "turn.started":
            raise CrashInjected("pause")

    first = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([]),
        FakeToolExecutor({}),
        store,
        fault_injector=inject,
    )
    with pytest.raises(CrashInjected):
        asyncio.run(first.run("run-001", [{"role": "user", "content": "alert"}]))

    cancelling = first.cancel("run-001")
    assert cancelling.status is RunStatus.CANCELLING

    provider = ScriptedStreamingProvider([])
    resumed = EventDrivenAgentRuntime(provider, FakeToolExecutor({}), store)
    cancelled = asyncio.run(resumed.resume("run-001"))

    assert cancelled.status is RunStatus.CANCELLED
    assert provider.requests == []
    assert [event.event_type for event in store.replay("run-001")][-2:] == [
        "run.cancelling",
        "run.cancelled",
    ]


def test_cancel_during_read_tool_waits_for_known_result_then_cancels(tmp_path) -> None:
    async def scenario():
        store = SQLiteEventStore(tmp_path / "events.db")
        started = asyncio.Event()
        release = asyncio.Event()
        call = ToolCall("logs-1", "query_logs", {})

        async def query(_call):
            started.set()
            await release.wait()
            return "logs"

        runtime = EventDrivenAgentRuntime(
            ScriptedStreamingProvider(
                [
                    events(
                        ProviderResponse(
                            stop_reason=StopReason.TOOL_USE, tool_calls=(call,)
                        )
                    )
                ]
            ),
            FakeToolExecutor({"logs-1": query}),
            store,
        )
        task = asyncio.create_task(
            runtime.run("run-001", [{"role": "user", "content": "alert"}])
        )
        await started.wait()
        assert runtime.cancel("run-001").status is RunStatus.CANCELLING
        release.set()
        result = await task
        return store, result

    store, result = asyncio.run(scenario())

    assert result.status is RunStatus.CANCELLED
    event_types = [event.event_type for event in store.replay("run-001")]
    assert event_types.index("run.cancelling") < event_types.index("tool.completed")
    assert event_types[-1] == "run.cancelled"


@pytest.mark.parametrize(
    ("response", "limits", "reason"),
    [
        (
            ProviderResponse(
                text="large",
                usage=ProviderUsage(input_tokens=8, output_tokens=5),
            ),
            RunLimits(max_tokens=12),
            "token budget exceeded: 13 > 12",
        ),
        (
            ProviderResponse(
                stop_reason=StopReason.TOOL_USE,
                tool_calls=(
                    ToolCall("one", "query", {}),
                    ToolCall("two", "query", {}),
                ),
            ),
            RunLimits(max_tool_calls=1),
            "tool call budget exceeded: 2 > 1",
        ),
    ],
)
def test_recoverable_runtime_preserves_budget_failures(
    tmp_path, response, limits, reason
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    executor = FakeToolExecutor({"one": "one", "two": "two"})
    runtime = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([events(response)]),
        executor,
        store,
        limits=limits,
    )

    result = asyncio.run(
        runtime.run("run-001", [{"role": "user", "content": "alert"}])
    )

    assert result.status is RunStatus.FAILED
    assert result.failure_reason == reason
    assert executor.calls == []

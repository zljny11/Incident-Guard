from __future__ import annotations

import asyncio

import pytest

from incident_guard.agents.provider import (
    ProviderEvent,
    ProviderResponse,
    ProviderUsage,
    StopReason,
    ToolCall,
)
from incident_guard.agents.react_runtime import (
    FakeToolExecutor,
    RunLimits,
    StructuredAgentRuntime,
)
from incident_guard.agents.run_models import RunStatus
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)


def scripted_events(response: ProviderResponse) -> list[ProviderEvent]:
    events = []
    if response.text:
        events.append(ProviderEvent.text_delta(response.text))
    events.extend(ProviderEvent.tool_call(call) for call in response.tool_calls)
    events.append(ProviderEvent.completed(response))
    return events


def run_runtime(provider, executor, *, limits=None):
    runtime = StructuredAgentRuntime(provider, executor, limits=limits)
    return asyncio.run(
        runtime.run("run-1", [{"role": "user", "content": "investigate"}])
    )


def test_direct_answer_completes_without_tools() -> None:
    response = ProviderResponse(text="service is healthy")
    provider = ScriptedStreamingProvider([scripted_events(response)])
    executor = FakeToolExecutor({})

    run = run_runtime(provider, executor)

    assert run.status is RunStatus.COMPLETED
    assert run.turns[0].final_response is response
    assert run.total_tool_calls == 0
    assert executor.calls == []


def test_multiple_tools_execute_in_original_order_and_feed_next_step() -> None:
    calls = (
        ToolCall("health-1", "query_service_health", {"service_id": "payment"}),
        ToolCall("metrics-1", "query_metrics", {"service_id": "payment"}),
    )
    tool_response = ProviderResponse(
        stop_reason=StopReason.TOOL_USE,
        tool_calls=calls,
    )
    final_response = ProviderResponse(text="deployment regression")
    provider = ScriptedStreamingProvider(
        [scripted_events(tool_response), scripted_events(final_response)]
    )
    executor = FakeToolExecutor(
        {"health-1": "unhealthy", "metrics-1": "error_rate=42%"}
    )

    run = run_runtime(provider, executor)

    assert run.status is RunStatus.COMPLETED
    assert [call.id for call in executor.calls] == ["health-1", "metrics-1"]
    assert [item.content for item in run.turns[0].steps[0].observations] == [
        "unhealthy",
        "error_rate=42%",
    ]
    second_request = provider.requests[1]
    assert [message["role"] for message in second_request[-3:]] == [
        "assistant",
        "tool",
        "tool",
    ]


def test_two_tool_steps_then_structured_final_result() -> None:
    health_call = ToolCall("health-1", "query_service_health", {})
    logs_call = ToolCall("logs-1", "query_logs", {})
    responses = [
        ProviderResponse(stop_reason="tool_use", tool_calls=(health_call,)),
        ProviderResponse(stop_reason="tool_use", tool_calls=(logs_call,)),
        ProviderResponse(text="root cause: bad deployment"),
    ]
    provider = ScriptedStreamingProvider([scripted_events(item) for item in responses])
    executor = FakeToolExecutor(
        {"health-1": "unhealthy", "logs-1": "exception after deploy"}
    )

    run = run_runtime(provider, executor)

    assert run.status is RunStatus.COMPLETED
    assert len(run.turns[0].steps) == 3
    assert run.total_tool_calls == 2
    assert run.turns[0].final_response.text == "root cause: bad deployment"


def test_step_budget_stops_a_repeating_tool_loop() -> None:
    calls = [ToolCall(f"call-{number}", "query_logs", {}) for number in (1, 2)]
    provider = ScriptedStreamingProvider(
        [
            scripted_events(
                ProviderResponse(stop_reason="tool_use", tool_calls=(call,))
            )
            for call in calls
        ]
    )
    executor = FakeToolExecutor({call.id: "still investigating" for call in calls})

    run = run_runtime(provider, executor, limits=RunLimits(max_steps=2))

    assert run.status is RunStatus.FAILED
    assert run.failure_reason == "step budget exhausted after 2 steps"
    assert len(executor.calls) == 2


def test_tool_budget_is_checked_before_executing_the_batch() -> None:
    calls = (
        ToolCall("call-1", "query_logs", {}),
        ToolCall("call-2", "query_metrics", {}),
    )
    response = ProviderResponse(stop_reason="tool_use", tool_calls=calls)
    provider = ScriptedStreamingProvider([scripted_events(response)])
    executor = FakeToolExecutor({call.id: "result" for call in calls})

    run = run_runtime(provider, executor, limits=RunLimits(max_tool_calls=1))

    assert run.status is RunStatus.FAILED
    assert run.failure_reason == "tool call budget exceeded: 2 > 1"
    assert executor.calls == []


def test_token_budget_produces_failed_terminal_state() -> None:
    response = ProviderResponse(
        text="large response",
        usage=ProviderUsage(input_tokens=8, output_tokens=5),
    )
    provider = ScriptedStreamingProvider([scripted_events(response)])

    run = run_runtime(
        provider,
        FakeToolExecutor({}),
        limits=RunLimits(max_tokens=12),
    )

    assert run.status is RunStatus.FAILED
    assert run.total_tokens == 13
    assert run.failure_reason == "token budget exceeded: 13 > 12"


def test_run_timeout_produces_failed_terminal_state() -> None:
    call = ToolCall("slow-1", "query_logs", {})
    response = ProviderResponse(stop_reason="tool_use", tool_calls=(call,))
    provider = ScriptedStreamingProvider([scripted_events(response)])

    async def slow_tool(_call):
        await asyncio.sleep(0.05)
        return "late"

    run = run_runtime(
        provider,
        FakeToolExecutor({"slow-1": slow_tool}),
        limits=RunLimits(timeout_seconds=0.001),
    )

    assert run.status is RunStatus.FAILED
    assert run.failure_reason == "run timeout exceeded after 0.001 seconds"


def test_provider_max_tokens_stop_is_a_failed_terminal_state() -> None:
    response = ProviderResponse(text="partial", stop_reason=StopReason.MAX_TOKENS)
    provider = ScriptedStreamingProvider([scripted_events(response)])

    run = run_runtime(provider, FakeToolExecutor({}))

    assert run.status is RunStatus.FAILED
    assert "provider stopped because max_tokens" in run.failure_reason


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_steps": 0},
        {"timeout_seconds": 0},
        {"max_tool_calls": -1},
        {"max_tokens": -1},
    ],
)
def test_run_limits_reject_invalid_values(kwargs) -> None:
    with pytest.raises(ValueError):
        RunLimits(**kwargs)

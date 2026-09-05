from __future__ import annotations

import asyncio

from incident_guard.agents.event_runtime import EventDrivenAgentRuntime
from incident_guard.agents.provider import (
    ProviderEvent,
    ProviderResponse,
    StopReason,
    ToolCall,
)
from incident_guard.agents.react_runtime import FakeToolExecutor
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)
from incident_guard.context import (
    ContextBudgetPolicy,
    ContextSnapshot,
    DeterministicTokenEstimator,
    PinReason,
)
from incident_guard.events import SQLiteEventStore


def make_snapshot() -> ContextSnapshot:
    messages = (
        {"role": "user", "content": "payment alert"},
        {
            "role": "assistant",
            "content": "old check",
            "tool_calls": [{"id": "old", "name": "logs", "arguments": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": "old",
            "name": "logs",
            "content": "duplicate log result " * 20,
            "is_error": False,
        },
        {
            "role": "assistant",
            "content": "new check",
            "tool_calls": [{"id": "new", "name": "logs", "arguments": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": "new",
            "name": "logs",
            "content": "duplicate log result " * 20,
            "is_error": False,
        },
        {"role": "assistant", "content": "latest hypothesis"},
    )
    estimator = DeterministicTokenEstimator()
    return ContextSnapshot(
        run_id="run-001",
        messages=messages,
        estimated_tokens=estimator.estimate_messages(messages),
        message_sources=(1, 2, 3, 4, 5, 6),
        pinned={1: PinReason.ALERT},
        source_sequences=(1, 2, 3, 4, 5, 6),
        last_sequence=10,
    )


def assert_no_orphan_tools(messages) -> None:
    requested: set[str] = set()
    observed: set[str] = set()
    for message in messages:
        for call in message.get("tool_calls", []):
            requested.add(call["id"])
        if message.get("role") == "tool":
            observed.add(message["tool_call_id"])
    assert requested == observed


def test_budget_trims_old_duplicate_group_and_preserves_complete_latest_pair() -> None:
    snapshot = make_snapshot()
    estimator = DeterministicTokenEstimator()
    desired = [
        snapshot.messages[index] for index in (0, 3, 4, 5)
    ]
    budget = estimator.estimate_messages(desired)

    trimmed = ContextBudgetPolicy(estimator).apply(snapshot, budget)

    assert trimmed.message_sources == (1, 4, 5, 6)
    assert trimmed.estimated_tokens <= budget
    assert trimmed.pinned == {1: PinReason.ALERT}
    assert_no_orphan_tools(trimmed.to_provider_messages())


def test_tighter_budget_drops_entire_tool_pair_without_orphan_result() -> None:
    snapshot = make_snapshot()
    estimator = DeterministicTokenEstimator()
    desired = [snapshot.messages[index] for index in (0, 5)]
    budget = estimator.estimate_messages(desired)

    trimmed = ContextBudgetPolicy(estimator).apply(snapshot, budget)

    assert trimmed.message_sources == (1, 6)
    assert trimmed.estimated_tokens <= budget
    assert_no_orphan_tools(trimmed.to_provider_messages())


def test_event_runtime_never_sends_provider_context_over_budget(tmp_path) -> None:
    call = ToolCall("logs-1", "query_logs", {})
    first = ProviderResponse(
        stop_reason=StopReason.TOOL_USE, tool_calls=(call,)
    )
    final = ProviderResponse(text="done")
    provider = ScriptedStreamingProvider(
        [
            [
                ProviderEvent.tool_call(call),
                ProviderEvent.completed(first),
            ],
            [
                ProviderEvent.text_delta("done"),
                ProviderEvent.completed(final),
            ],
        ]
    )
    estimator = DeterministicTokenEstimator()
    budget = 80
    runtime = EventDrivenAgentRuntime(
        provider,
        FakeToolExecutor({"logs-1": "very long logs " * 200}),
        SQLiteEventStore(tmp_path / "events.db"),
        context_token_budget=budget,
    )

    result = asyncio.run(
        runtime.run("run-001", [{"role": "user", "content": "alert"}])
    )

    assert result.status.value == "completed"
    assert all(
        estimator.estimate_messages(request) <= budget
        for request in provider.requests
    )
    assert_no_orphan_tools(provider.requests[1])

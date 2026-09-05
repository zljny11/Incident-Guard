from __future__ import annotations

import asyncio

import pytest

from incident_guard.agents.event_runtime import CrashInjected, EventDrivenAgentRuntime
from incident_guard.agents.provider import ProviderEvent, ProviderResponse
from incident_guard.agents.react_runtime import FakeToolExecutor
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)
from incident_guard.events import InboxKind, InboxTarget, SQLiteEventStore


def events(response: ProviderResponse):
    result = []
    if response.text:
        result.append(ProviderEvent.text_delta(response.text))
    result.append(ProviderEvent.completed(response))
    return result


@pytest.mark.parametrize(
    ("kind", "content"),
    [
        (InboxKind.STEERING, "focus on deployment timing"),
        (InboxKind.INJECTED_CONTEXT, "deployment v2 started at 10:30"),
    ],
)
def test_next_step_input_is_consumed_once_after_resume(
    tmp_path, kind: InboxKind, content: str
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")

    def crash_at_turn(event) -> None:
        if event.event_type == "turn.started":
            raise CrashInjected("pause")

    first = EventDrivenAgentRuntime(
        ScriptedStreamingProvider([]),
        FakeToolExecutor({}),
        store,
        fault_injector=crash_at_turn,
    )
    with pytest.raises(CrashInjected):
        asyncio.run(first.run("run-001", [{"role": "user", "content": "alert"}]))

    steering = first.inbox.submit(
        "run-001",
        content,
        kind=kind,
        target=InboxTarget.NEXT_STEP,
    )
    provider = ScriptedStreamingProvider([events(ProviderResponse(text="done"))])
    resumed = EventDrivenAgentRuntime(provider, FakeToolExecutor({}), store)

    result = asyncio.run(resumed.resume("run-001"))

    assert result.status.value == "completed"
    assert provider.requests[0][-1] == {
        "role": "system",
        "content": content,
    }
    consumed = [
        event
        for event in store.replay("run-001")
        if event.event_type == "inbox.consumed"
        and event.payload["message_id"] == steering.message_id
    ]
    assert len(consumed) == 1


def test_next_turn_follow_up_waits_for_turn_boundary(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")

    def crash_after_step(event) -> None:
        if event.event_type == "step.completed":
            raise CrashInjected("pause")

    first_provider = ScriptedStreamingProvider(
        [events(ProviderResponse(text="first answer"))]
    )
    first = EventDrivenAgentRuntime(
        first_provider,
        FakeToolExecutor({}),
        store,
        fault_injector=crash_after_step,
    )
    with pytest.raises(CrashInjected):
        asyncio.run(first.run("run-001", [{"role": "user", "content": "alert"}]))

    first.inbox.submit(
        "run-001",
        "please verify again",
        kind=InboxKind.FOLLOW_UP,
        target=InboxTarget.NEXT_TURN,
    )
    second_provider = ScriptedStreamingProvider(
        [events(ProviderResponse(text="verified"))]
    )
    resumed = EventDrivenAgentRuntime(
        second_provider, FakeToolExecutor({}), store
    )

    result = asyncio.run(resumed.resume("run-001"))

    assert result.status.value == "completed"
    assert second_provider.requests[0][-1] == {
        "role": "user",
        "content": "please verify again",
    }
    assert result.turn_number == 2

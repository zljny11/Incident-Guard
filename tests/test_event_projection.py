from __future__ import annotations

import pytest

from incident_guard.agents.run_models import RunStatus
from incident_guard.events import (
    NewRunEvent,
    ProjectionError,
    RunEventProjector,
    SQLiteEventStore,
    ToolState,
)


def append_valid_tool_run(store, run_id: str = "run-001") -> None:
    store.append_batch(
        run_id,
        (
            NewRunEvent("run.started"),
            NewRunEvent("operator.message", {"role": "user", "content": "alert"}),
            NewRunEvent("turn.started", {"turn_number": 1}),
            NewRunEvent(
                "step.started", {"turn_number": 1, "step_number": 1}
            ),
            NewRunEvent(
                "assistant.message",
                {
                    "turn_number": 1,
                    "step_number": 1,
                    "text": "checking",
                    "stop_reason": "tool_use",
                    "tool_calls": [
                        {"id": "health-1", "name": "health", "arguments": {}}
                    ],
                    "input_tokens": 2,
                    "output_tokens": 1,
                },
            ),
            NewRunEvent(
                "tool.requested",
                {
                    "call_id": "health-1",
                    "name": "health",
                    "arguments": {},
                    "effect": "read",
                    "call_index": 0,
                },
            ),
            NewRunEvent(
                "tool.started",
                {"call_id": "health-1", "name": "health", "effect": "read"},
            ),
            NewRunEvent(
                "tool.completed",
                {
                    "call_id": "health-1",
                    "name": "health",
                    "content": "unhealthy",
                    "is_error": False,
                },
            ),
            NewRunEvent(
                "step.completed", {"turn_number": 1, "step_number": 1}
            ),
        ),
    )


def test_projection_rebuilds_run_messages_and_tool_state_deterministically(
    tmp_path,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    append_valid_tool_run(store)
    projector = RunEventProjector()

    first = projector.project("run-001", store.replay("run-001"))
    second = projector.project("run-001", store.replay("run-001"))

    assert first == second
    assert first.status is RunStatus.RUNNING
    assert [message["role"] for message in first.provider_messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert first.tools["health-1"].state is ToolState.COMPLETED
    assert first.tools["health-1"].content == "unhealthy"
    assert first.completed_steps == ((1, 1),)


def test_projection_rejects_execution_events_after_terminal_run(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    store.append_batch(
        "run-001",
        (
            NewRunEvent("run.started"),
            NewRunEvent("run.completed"),
            NewRunEvent("turn.started", {"turn_number": 1}),
        ),
    )

    with pytest.raises(ProjectionError, match="terminal run"):
        RunEventProjector().project("run-001", store.replay("run-001"))


def test_projection_requires_contiguous_sequence() -> None:
    from incident_guard.events import RunEvent

    event = RunEvent(
        event_id="event-2",
        run_id="run-001",
        sequence=2,
        event_type="run.started",
        payload={},
        schema_version=1,
        occurred_at=1.0,
    )

    with pytest.raises(ProjectionError, match="expected event sequence 1"):
        RunEventProjector().project("run-001", (event,))

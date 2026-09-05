from __future__ import annotations

from copy import deepcopy

import pytest

from incident_guard.context import (
    DeterministicTokenEstimator,
    EventContextProjector,
)
from incident_guard.events import NewRunEvent, SQLiteEventStore


def append_context_stream(store) -> None:
    store.append_batch(
        "run-001",
        (
            NewRunEvent("run.started"),
            NewRunEvent(
                "operator.message", {"role": "user", "content": "payment alert"}
            ),
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
                        {
                            "id": "health-1",
                            "name": "query_health",
                            "arguments": {"service_id": "payment"},
                        }
                    ],
                    "input_tokens": 4,
                    "output_tokens": 2,
                },
            ),
            NewRunEvent(
                "tool.requested",
                {
                    "call_id": "health-1",
                    "name": "query_health",
                    "arguments": {"service_id": "payment"},
                    "effect": "read",
                    "call_index": 0,
                },
            ),
            NewRunEvent(
                "tool.started",
                {
                    "call_id": "health-1",
                    "name": "query_health",
                    "effect": "read",
                },
            ),
            NewRunEvent(
                "tool.completed",
                {
                    "call_id": "health-1",
                    "name": "query_health",
                    "content": "unhealthy",
                    "is_error": False,
                },
            ),
            NewRunEvent(
                "step.completed", {"turn_number": 1, "step_number": 1}
            ),
        ),
    )


def test_same_event_stream_produces_same_context_snapshot(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    append_context_stream(store)
    projector = EventContextProjector()
    events = store.replay("run-001")

    first = projector.project("run-001", events)
    second = projector.project("run-001", events)

    assert first == second
    assert first.last_sequence == len(events)
    assert first.source_sequences == (2, 5, 8)
    assert [message["role"] for message in first.messages] == [
        "user",
        "assistant",
        "tool",
    ]


def test_projection_preserves_tool_call_result_order_and_source_events(
    tmp_path,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    append_context_stream(store)
    original_events = store.replay("run-001")
    original_payloads = deepcopy([dict(event.payload) for event in original_events])

    snapshot = EventContextProjector().project("run-001", original_events)
    messages = snapshot.to_provider_messages()

    assert messages[1]["tool_calls"][0]["id"] == "health-1"
    assert messages[2]["tool_call_id"] == "health-1"
    assert [dict(event.payload) for event in original_events] == original_payloads
    messages[0]["content"] = "mutated copy"
    assert snapshot.messages[0]["content"] == "payment alert"


def test_token_estimation_is_deterministic_and_provider_independent() -> None:
    estimator = DeterministicTokenEstimator()
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "你好"},
    ]

    assert estimator.estimate_messages(messages) == estimator.estimate_messages(
        deepcopy(messages)
    )
    assert estimator.estimate_text("你好") > estimator.estimate_text("hi")
    assert estimator.estimate_messages(messages) > sum(
        estimator.estimate_text(message["content"]) for message in messages
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"bytes_per_token": 0},
        {"message_overhead": 0},
        {"conversation_overhead": 0},
    ],
)
def test_token_estimator_rejects_invalid_configuration(kwargs) -> None:
    with pytest.raises(ValueError):
        DeterministicTokenEstimator(**kwargs)

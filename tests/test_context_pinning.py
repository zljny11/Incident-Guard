from __future__ import annotations

import pytest

from incident_guard.context import (
    DeterministicTokenEstimator,
    EventContextProjector,
    PinReason,
    PinnedContextOverflow,
)
from incident_guard.events import NewRunEvent, SQLiteEventStore


def build_pinned_stream(store) -> None:
    store.append_batch(
        "run-001",
        (
            NewRunEvent("run.started"),
            NewRunEvent(
                "alert.received",
                {"role": "user", "content": "payment error rate is 42%"},
            ),
            NewRunEvent(
                "goal.set",
                {"role": "system", "content": "restore payment safely"},
            ),
            NewRunEvent(
                "operator.message",
                {"role": "user", "content": "old operator note"},
            ),
            NewRunEvent(
                "evidence.recorded",
                {
                    "role": "system",
                    "content": "evidence: errors started after deployment v2",
                    "evidence_id": "evidence-1",
                },
            ),
            NewRunEvent(
                "operator.message",
                {"role": "user", "content": "latest operator instruction"},
            ),
            NewRunEvent("turn.started", {"turn_number": 1}),
        ),
    )


def test_alert_goal_latest_operator_and_evidence_are_pinned(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    build_pinned_stream(store)

    snapshot = EventContextProjector().project(
        "run-001", store.replay("run-001")
    )

    assert snapshot.pinned == {
        2: PinReason.ALERT,
        3: PinReason.GOAL,
        5: PinReason.EVIDENCE,
        6: PinReason.LATEST_OPERATOR_INPUT,
    }
    assert 4 not in snapshot.pinned
    assert [message["content"] for message in snapshot.pinned_messages] == [
        "payment error rate is 42%",
        "restore payment safely",
        "evidence: errors started after deployment v2",
        "latest operator instruction",
    ]


def test_pin_selection_is_deterministic_and_traceable(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    build_pinned_stream(store)
    events = store.replay("run-001")
    projector = EventContextProjector()

    first = projector.project("run-001", events)
    second = projector.project("run-001", events)

    assert first == second
    assert set(first.pinned).issubset(first.source_sequences)
    assert tuple(first.pinned) == tuple(sorted(first.pinned))


def test_impossibly_small_budget_fails_without_dropping_pins(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    build_pinned_stream(store)
    estimator = DeterministicTokenEstimator()
    snapshot = EventContextProjector(estimator).project(
        "run-001", store.replay("run-001")
    )
    pinned_before = snapshot.pinned_messages

    with pytest.raises(PinnedContextOverflow, match="pinned context exceeds"):
        snapshot.require_pinned_within(1, estimator)

    assert snapshot.pinned_messages == pinned_before
    snapshot.require_pinned_within(10_000, estimator)

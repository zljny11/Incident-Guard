from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from incident_guard.events import (
    CURRENT_EVENT_SCHEMA_VERSION,
    NewRunEvent,
    SQLiteEventStore,
)


def test_append_and_replay_preserve_durable_event_fields(tmp_path) -> None:
    database = tmp_path / "events.db"
    event = NewRunEvent(
        event_type="run.started",
        payload={"alert": "payment 5xx", "labels": ["critical"]},
        occurred_at=1234.5,
    )

    with SQLiteEventStore(database) as store:
        stored = store.append("run-001", event)

    with SQLiteEventStore(database) as reopened:
        replayed = reopened.replay("run-001")

    assert replayed == (stored,)
    assert stored.sequence == 1
    assert stored.event_id == event.event_id
    assert stored.schema_version == CURRENT_EVENT_SCHEMA_VERSION
    assert stored.payload == {"alert": "payment 5xx", "labels": ["critical"]}


def test_sequence_is_monotonic_per_run_and_replay_is_ordered(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")

    first_batch = store.append_batch(
        "run-a",
        (
            NewRunEvent("run.started"),
            NewRunEvent("turn.started", {"turn": 1}),
        ),
    )
    other_run = store.append("run-b", NewRunEvent("run.started"))
    last = store.append("run-a", NewRunEvent("step.started", {"step": 1}))

    assert [event.sequence for event in first_batch] == [1, 2]
    assert other_run.sequence == 1
    assert last.sequence == 3
    assert [event.event_type for event in store.replay("run-a")] == [
        "run.started",
        "turn.started",
        "step.started",
    ]
    assert store.replay("run-a", after_sequence=1) == (first_batch[1], last)


def test_failed_batch_is_fully_rolled_back_without_consuming_sequence(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    duplicate_id = "duplicate-event"

    with pytest.raises(sqlite3.IntegrityError):
        store.append_batch(
            "run-001",
            (
                NewRunEvent("run.started", event_id=duplicate_id),
                NewRunEvent("turn.started", event_id=duplicate_id),
            ),
        )

    assert store.replay("run-001") == ()
    stored = store.append("run-001", NewRunEvent("run.started"))
    assert stored.sequence == 1


def test_database_rejects_update_and_delete_of_stored_events(tmp_path) -> None:
    database = tmp_path / "events.db"
    with SQLiteEventStore(database) as store:
        store.append("run-001", NewRunEvent("run.started"))

    connection = sqlite3.connect(database)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE run_events SET event_type = 'changed' WHERE run_id = 'run-001'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM run_events WHERE run_id = 'run-001'")
    connection.close()


def test_concurrent_store_instances_allocate_unique_monotonic_sequences(
    tmp_path,
) -> None:
    database = tmp_path / "events.db"
    stores = [SQLiteEventStore(database) for _ in range(4)]

    def append(index: int) -> int:
        event = stores[index % len(stores)].append(
            "run-001", NewRunEvent("operator.message", {"index": index})
        )
        return event.sequence

    with ThreadPoolExecutor(max_workers=4) as executor:
        assigned = list(executor.map(append, range(20)))

    assert sorted(assigned) == list(range(1, 21))
    assert [event.sequence for event in stores[0].replay("run-001")] == list(
        range(1, 21)
    )
    for store in stores:
        store.close()


def test_event_validation_happens_before_writing(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")

    with pytest.raises(ValueError, match="JSON serializable"):
        NewRunEvent("run.started", {"bad": object()})
    with pytest.raises(ValueError, match="schema_version"):
        NewRunEvent("run.started", schema_version=0)
    with pytest.raises(ValueError, match="run_id"):
        store.append(" ", NewRunEvent("run.started"))

    assert store.replay("run-001") == ()

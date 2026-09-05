from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from time import time
from typing import Protocol, runtime_checkable
from uuid import uuid4


CURRENT_EVENT_SCHEMA_VERSION = 1
_DATABASE_SCHEMA_VERSION = 1


def _require_non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _copy_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("event payload must be a mapping")
    if not all(isinstance(key, str) for key in payload):
        raise ValueError("event payload keys must be strings")

    copied = dict(payload)
    try:
        encoded = json.dumps(
            copied,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("event payload must be JSON serializable") from error
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict):  # Defensive: mappings must remain JSON objects.
        raise ValueError("event payload must serialize to a JSON object")
    return decoded


@dataclass(frozen=True, slots=True)
class NewRunEvent:
    """A durable event before the store assigns its per-run sequence."""

    event_type: str
    payload: Mapping[str, object] = field(default_factory=dict)
    schema_version: int = CURRENT_EVENT_SCHEMA_VERSION
    event_id: str = field(default_factory=lambda: str(uuid4()))
    occurred_at: float = field(default_factory=time)

    def __post_init__(self) -> None:
        _require_non_empty(self.event_type, "event_type")
        _require_non_empty(self.event_id, "event_id")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive int")
        if type(self.occurred_at) not in {int, float} or not math.isfinite(
            self.occurred_at
        ):
            raise ValueError("occurred_at must be a finite timestamp")
        object.__setattr__(self, "payload", _copy_payload(self.payload))
        object.__setattr__(self, "occurred_at", float(self.occurred_at))


@dataclass(frozen=True, slots=True)
class RunEvent:
    """A versioned durable event with a store-assigned sequence."""

    event_id: str
    run_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, object]
    schema_version: int
    occurred_at: float

    def __post_init__(self) -> None:
        _require_non_empty(self.event_id, "event_id")
        _require_non_empty(self.run_id, "run_id")
        _require_non_empty(self.event_type, "event_type")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequence must be a positive int")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("schema_version must be a positive int")
        if type(self.occurred_at) not in {int, float} or not math.isfinite(
            self.occurred_at
        ):
            raise ValueError("occurred_at must be a finite timestamp")
        object.__setattr__(self, "payload", _copy_payload(self.payload))
        object.__setattr__(self, "occurred_at", float(self.occurred_at))


@runtime_checkable
class EventStore(Protocol):
    """Persistence boundary for append-only durable run events."""

    def append(self, run_id: str, event: NewRunEvent) -> RunEvent: ...

    def append_batch(
        self, run_id: str, events: Iterable[NewRunEvent]
    ) -> tuple[RunEvent, ...]: ...

    def replay(
        self, run_id: str, *, after_sequence: int = 0
    ) -> tuple[RunEvent, ...]: ...


class SQLiteEventStore:
    """SQLite-backed append-only store with monotonic per-run sequences."""

    def __init__(self, database: str | Path) -> None:
        database_name = str(database)
        if not database_name:
            raise ValueError("database path must be non-empty")
        if database_name != ":memory:":
            Path(database_name).parent.mkdir(parents=True, exist_ok=True)

        self._lock = RLock()
        self._closed = False
        self._connection = sqlite3.connect(
            database_name,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        try:
            self._initialize_schema()
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def _initialize_schema(self) -> None:
        current_version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version not in {0, _DATABASE_SCHEMA_VERSION}:
            raise RuntimeError(
                "Unsupported event store database schema version: "
                f"{current_version}"
            )

        self._connection.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS run_events (
                run_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                event_id TEXT NOT NULL UNIQUE,
                event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
                schema_version INTEGER NOT NULL CHECK (schema_version > 0),
                occurred_at REAL NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (run_id, sequence)
            );

            CREATE TRIGGER IF NOT EXISTS run_events_forbid_update
            BEFORE UPDATE ON run_events
            BEGIN
                SELECT RAISE(ABORT, 'run_events is append-only');
            END;

            CREATE TRIGGER IF NOT EXISTS run_events_forbid_delete
            BEFORE DELETE ON run_events
            BEGIN
                SELECT RAISE(ABORT, 'run_events is append-only');
            END;

            PRAGMA user_version = {_DATABASE_SCHEMA_VERSION};
            """
        )

    def append(self, run_id: str, event: NewRunEvent) -> RunEvent:
        appended = self.append_batch(run_id, (event,))
        return appended[0]

    def append_batch(
        self, run_id: str, events: Iterable[NewRunEvent]
    ) -> tuple[RunEvent, ...]:
        normalized_run_id = _require_non_empty(run_id, "run_id")
        pending = tuple(events)
        if not all(isinstance(event, NewRunEvent) for event in pending):
            raise ValueError("events must contain NewRunEvent values")
        if not pending:
            return ()

        encoded_payloads = tuple(
            json.dumps(
                event.payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            for event in pending
        )

        with self._lock:
            self._ensure_open()
            try:
                # This write lock covers both sequence allocation and the full batch.
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0)
                    FROM run_events
                    WHERE run_id = ?
                    """,
                    (normalized_run_id,),
                ).fetchone()
                first_sequence = int(row[0]) + 1
                stored: list[RunEvent] = []
                for offset, (event, payload_json) in enumerate(
                    zip(pending, encoded_payloads, strict=True)
                ):
                    sequence = first_sequence + offset
                    self._connection.execute(
                        """
                        INSERT INTO run_events (
                            run_id, sequence, event_id, event_type,
                            schema_version, occurred_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            normalized_run_id,
                            sequence,
                            event.event_id,
                            event.event_type,
                            event.schema_version,
                            event.occurred_at,
                            payload_json,
                        ),
                    )
                    stored.append(
                        RunEvent(
                            event_id=event.event_id,
                            run_id=normalized_run_id,
                            sequence=sequence,
                            event_type=event.event_type,
                            payload=event.payload,
                            schema_version=event.schema_version,
                            occurred_at=event.occurred_at,
                        )
                    )
                self._connection.execute("COMMIT")
                return tuple(stored)
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def replay(
        self, run_id: str, *, after_sequence: int = 0
    ) -> tuple[RunEvent, ...]:
        normalized_run_id = _require_non_empty(run_id, "run_id")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("after_sequence must be a non-negative int")

        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT event_id, run_id, sequence, event_type,
                       payload_json, schema_version, occurred_at
                FROM run_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (normalized_run_id, after_sequence),
            ).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    def list_run_ids(self) -> tuple[str, ...]:
        """Return run IDs ordered by their most recent durable event."""

        with self._lock:
            self._ensure_open()
            rows = self._connection.execute(
                """
                SELECT run_id, MAX(occurred_at) AS last_event_at
                FROM run_events
                GROUP BY run_id
                ORDER BY last_event_at DESC, run_id ASC
                """
            ).fetchall()
        return tuple(str(row["run_id"]) for row in rows)

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> RunEvent:
        return RunEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            sequence=row["sequence"],
            event_type=row["event_type"],
            payload=json.loads(row["payload_json"]),
            schema_version=row["schema_version"],
            occurred_at=row["occurred_at"],
        )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("event store is closed")

    def __enter__(self) -> SQLiteEventStore:
        self._ensure_open()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

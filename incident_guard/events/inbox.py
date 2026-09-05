from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import RLock
from uuid import uuid4

from incident_guard.agents.run_models import RunStatus
from incident_guard.events.event_store import EventStore, NewRunEvent
from incident_guard.events.projection import RunEventProjector, RunProjection


class InboxKind(StrEnum):
    STEERING = "steering"
    FOLLOW_UP = "follow_up"
    INJECTED_CONTEXT = "injected_context"


class InboxTarget(StrEnum):
    NEXT_STEP = "next_step"
    NEXT_TURN = "next_turn"


@dataclass(frozen=True, slots=True)
class InboxMessage:
    message_id: str
    kind: InboxKind
    target: InboxTarget
    content: str
    role: str


class DurableRunInbox:
    """Durable inbox whose messages are consumed exactly once at a boundary."""

    def __init__(
        self, event_store: EventStore, projector: RunEventProjector | None = None
    ) -> None:
        self.event_store = event_store
        self.projector = projector or RunEventProjector()
        self._lock = RLock()

    def submit(
        self,
        run_id: str,
        content: str,
        *,
        kind: InboxKind = InboxKind.STEERING,
        target: InboxTarget = InboxTarget.NEXT_STEP,
        role: str | None = None,
        message_id: str | None = None,
    ) -> InboxMessage:
        normalized_kind = InboxKind(kind)
        normalized_target = InboxTarget(target)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("inbox content must be non-empty")
        normalized_role = role or (
            "user" if normalized_kind is InboxKind.FOLLOW_UP else "system"
        )
        if not isinstance(normalized_role, str) or not normalized_role.strip():
            raise ValueError("inbox role must be non-empty")
        normalized_id = message_id or str(uuid4())
        if not isinstance(normalized_id, str) or not normalized_id.strip():
            raise ValueError("message_id must be non-empty")

        with self._lock:
            projection = self.projection(run_id)
            if projection.status is not RunStatus.RUNNING:
                raise ValueError("inbox input requires a running run")
            message = InboxMessage(
                message_id=normalized_id,
                kind=normalized_kind,
                target=normalized_target,
                content=content,
                role=normalized_role,
            )
            self.event_store.append(
                run_id,
                NewRunEvent(
                    "inbox.message",
                    {
                        "message_id": message.message_id,
                        "kind": message.kind.value,
                        "target": message.target.value,
                        "role": message.role,
                        "content": message.content,
                    },
                ),
            )
            return message

    def consume(
        self, run_id: str, target: InboxTarget
    ) -> tuple[InboxMessage, ...]:
        normalized_target = InboxTarget(target)
        with self._lock:
            projection = self.projection(run_id)
            if projection.status is not RunStatus.RUNNING:
                raise ValueError("inbox consumption requires a running run")
            pending = tuple(
                InboxMessage(
                    message_id=item.message_id,
                    kind=InboxKind(item.kind),
                    target=InboxTarget(item.target),
                    content=item.content,
                    role=item.role,
                )
                for item in projection.inbox_items
                if not item.consumed and item.target == normalized_target.value
            )
            self.event_store.append_batch(
                run_id,
                (
                    NewRunEvent(
                        "inbox.consumed",
                        {
                            "message_id": item.message_id,
                            "boundary": normalized_target.value,
                        },
                    )
                    for item in pending
                ),
            )
            return pending

    def projection(self, run_id: str) -> RunProjection:
        return self.projector.project(run_id, self.event_store.replay(run_id))

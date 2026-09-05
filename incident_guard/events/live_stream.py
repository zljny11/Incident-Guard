from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from time import time
from typing import Any


@dataclass(frozen=True, slots=True)
class LiveEvent:
    """Ephemeral UI event. Live events are never written to EventStore."""

    run_id: str
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: float = field(default_factory=time)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("LiveEvent run_id must be non-empty")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise ValueError("LiveEvent event_type must be non-empty")
        if not isinstance(self.payload, Mapping):
            raise ValueError("LiveEvent payload must be a mapping")
        object.__setattr__(self, "payload", dict(self.payload))


_CLOSED = object()


class LiveEventSubscription:
    def __init__(self, broker: LiveEventBroker, run_id: str) -> None:
        self._broker = broker
        self._run_id = run_id
        self._queue: asyncio.Queue[LiveEvent | object] = asyncio.Queue()
        self._closed = False

    def __aiter__(self) -> AsyncIterator[LiveEvent]:
        return self

    async def __anext__(self) -> LiveEvent:
        item = await self._queue.get()
        if item is _CLOSED:
            self._closed = True
            raise StopAsyncIteration
        assert isinstance(item, LiveEvent)
        return item

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._broker._unsubscribe(self._run_id, self)
            self._queue.put_nowait(_CLOSED)


class LiveEventBroker:
    """Process-local fan-out for deltas, progress, and runtime status."""

    def __init__(self) -> None:
        self._subscriptions: dict[str, set[LiveEventSubscription]] = {}

    def subscribe(self, run_id: str) -> LiveEventSubscription:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be non-empty")
        subscription = LiveEventSubscription(self, run_id)
        self._subscriptions.setdefault(run_id, set()).add(subscription)
        return subscription

    async def emit(self, event: LiveEvent) -> None:
        if not isinstance(event, LiveEvent):
            raise ValueError("emit requires a LiveEvent")
        for subscription in tuple(self._subscriptions.get(event.run_id, ())):
            subscription._queue.put_nowait(event)

    def close_run(self, run_id: str) -> None:
        for subscription in tuple(self._subscriptions.get(run_id, ())):
            subscription.close()

    def _unsubscribe(
        self, run_id: str, subscription: LiveEventSubscription
    ) -> None:
        subscriptions = self._subscriptions.get(run_id)
        if subscriptions is None:
            return
        subscriptions.discard(subscription)
        if not subscriptions:
            self._subscriptions.pop(run_id, None)

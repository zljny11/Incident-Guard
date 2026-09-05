from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from incident_guard.events import RunEvent, RunEventProjector


def _copy_json_object(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        copied = json.loads(
            json.dumps(
                dict(value),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError("provider message must be JSON serializable") from error
    if not isinstance(copied, dict):
        raise ValueError("provider message must be a JSON object")
    return copied


@runtime_checkable
class TokenEstimator(Protocol):
    """Provider-independent deterministic token estimation boundary."""

    def estimate_text(self, text: str) -> int: ...

    def estimate_message(self, message: Mapping[str, Any]) -> int: ...

    def estimate_messages(self, messages: Sequence[Mapping[str, Any]]) -> int: ...


class PinReason(StrEnum):
    ALERT = "alert"
    GOAL = "goal"
    LATEST_OPERATOR_INPUT = "latest_operator_input"
    EVIDENCE = "evidence"


class PinnedContextOverflow(ValueError):
    """The budget is too small for context that must never be discarded."""


class ContextBudgetOverflow(ValueError):
    """No structurally valid provider context can fit the budget."""


@dataclass(frozen=True, slots=True)
class DeterministicTokenEstimator:
    """Stable UTF-8 approximation used before provider-specific tokenization."""

    bytes_per_token: int = 4
    message_overhead: int = 4
    conversation_overhead: int = 2

    def __post_init__(self) -> None:
        for name, value in (
            ("bytes_per_token", self.bytes_per_token),
            ("message_overhead", self.message_overhead),
            ("conversation_overhead", self.conversation_overhead),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive int")

    def estimate_text(self, text: str) -> int:
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        byte_count = len(text.encode("utf-8"))
        if byte_count == 0:
            return 0
        return (byte_count + self.bytes_per_token - 1) // self.bytes_per_token

    def estimate_message(self, message: Mapping[str, Any]) -> int:
        if not isinstance(message, Mapping):
            raise ValueError("message must be a mapping")
        canonical = json.dumps(
            _copy_json_object(message),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return self.message_overhead + self.estimate_text(canonical)

    def estimate_messages(self, messages: Sequence[Mapping[str, Any]]) -> int:
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
            raise ValueError("messages must be a sequence")
        return self.conversation_overhead + sum(
            self.estimate_message(message) for message in messages
        )


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Immutable provider context derived from one durable event prefix."""

    run_id: str
    messages: tuple[Mapping[str, Any], ...]
    estimated_tokens: int
    message_sources: tuple[int, ...]
    pinned: Mapping[int, PinReason]
    source_sequences: tuple[int, ...]
    last_sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("ContextSnapshot run_id must be non-empty")
        if type(self.estimated_tokens) is not int or self.estimated_tokens < 0:
            raise ValueError("estimated_tokens must be a non-negative int")
        if type(self.last_sequence) is not int or self.last_sequence < 0:
            raise ValueError("last_sequence must be a non-negative int")
        if not isinstance(self.messages, tuple):
            object.__setattr__(self, "messages", tuple(self.messages))
        copied_messages = tuple(
            MappingProxyType(_copy_json_object(message)) for message in self.messages
        )
        object.__setattr__(self, "messages", copied_messages)
        if not isinstance(self.message_sources, tuple):
            object.__setattr__(
                self, "message_sources", tuple(self.message_sources)
            )
        if len(self.message_sources) != len(self.messages):
            raise ValueError("message_sources must align with messages")
        if any(
            type(sequence) is not int or sequence < 1
            for sequence in self.message_sources
        ):
            raise ValueError("message_sources must contain positive ints")
        normalized_pins: dict[int, PinReason] = {}
        for sequence, reason in self.pinned.items():
            if type(sequence) is not int or sequence < 1:
                raise ValueError("pinned sequence must be a positive int")
            normalized_pins[sequence] = PinReason(reason)
        if not set(normalized_pins).issubset(self.message_sources):
            raise ValueError("pinned sequences must refer to projected messages")
        object.__setattr__(self, "pinned", MappingProxyType(normalized_pins))
        if not isinstance(self.source_sequences, tuple):
            object.__setattr__(
                self, "source_sequences", tuple(self.source_sequences)
            )
        if any(
            type(sequence) is not int or sequence < 1
            for sequence in self.source_sequences
        ):
            raise ValueError("source_sequences must contain positive ints")
        if tuple(sorted(set(self.source_sequences))) != self.source_sequences:
            raise ValueError("source_sequences must be unique and ordered")
        if self.source_sequences and self.source_sequences[-1] > self.last_sequence:
            raise ValueError("source sequence exceeds last_sequence")

    def to_provider_messages(self) -> list[dict[str, Any]]:
        return [_copy_json_object(message) for message in self.messages]

    @property
    def pinned_messages(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            message
            for message, sequence in zip(
                self.messages, self.message_sources, strict=True
            )
            if sequence in self.pinned
        )

    def require_pinned_within(
        self, token_budget: int, estimator: TokenEstimator
    ) -> None:
        if type(token_budget) is not int or token_budget < 1:
            raise ValueError("token_budget must be a positive int")
        pinned_tokens = estimator.estimate_messages(self.pinned_messages)
        if pinned_tokens > token_budget:
            raise PinnedContextOverflow(
                "pinned context exceeds token budget: "
                f"{pinned_tokens} > {token_budget}"
            )


class EventContextProjector:
    """Pure durable-event to provider-context projection."""

    def __init__(
        self,
        estimator: TokenEstimator | None = None,
        run_projector: RunEventProjector | None = None,
    ) -> None:
        self.estimator = estimator or DeterministicTokenEstimator()
        if not isinstance(self.estimator, TokenEstimator):
            raise ValueError("estimator must implement TokenEstimator")
        self.run_projector = run_projector or RunEventProjector()

    def project(
        self, run_id: str, events: tuple[RunEvent, ...]
    ) -> ContextSnapshot:
        durable_events = tuple(events)
        run = self.run_projector.project(run_id, durable_events)
        messages = tuple(run.provider_messages)
        estimated_tokens = self.estimator.estimate_messages(messages)
        message_events = tuple(
            event
            for event in durable_events
            if event.event_type
            in {
                "operator.message",
                "context.injected",
                "alert.received",
                "goal.set",
                "evidence.recorded",
                "assistant.message",
                "tool.completed",
                "tool.failed",
                "inbox.consumed",
            }
        )
        message_sources = tuple(event.sequence for event in message_events)
        if len(message_sources) != len(messages):
            raise ValueError("message source projection is not aligned")
        source_sequences = message_sources
        pinned = self._select_pins(message_events)
        return ContextSnapshot(
            run_id=run_id,
            messages=messages,
            estimated_tokens=estimated_tokens,
            message_sources=message_sources,
            pinned=pinned,
            source_sequences=source_sequences,
            last_sequence=run.last_sequence,
        )

    @staticmethod
    def _select_pins(events: tuple[RunEvent, ...]) -> dict[int, PinReason]:
        pinned: dict[int, PinReason] = {}
        latest_by_type: dict[str, RunEvent] = {}
        for event in events:
            if event.event_type in {
                "alert.received",
                "goal.set",
                "operator.message",
                "inbox.consumed",
            }:
                category = (
                    "operator"
                    if event.event_type in {"operator.message", "inbox.consumed"}
                    else event.event_type
                )
                latest_by_type[category] = event
            elif event.event_type == "evidence.recorded":
                pinned[event.sequence] = PinReason.EVIDENCE

        reasons = {
            "alert.received": PinReason.ALERT,
            "goal.set": PinReason.GOAL,
            "operator": PinReason.LATEST_OPERATOR_INPUT,
        }
        for category, reason in reasons.items():
            event = latest_by_type.get(category)
            if event is not None:
                pinned[event.sequence] = reason
        return dict(sorted(pinned.items()))


@dataclass(frozen=True, slots=True)
class _MessageGroup:
    indexes: tuple[int, ...]
    complete: bool = True


class ContextBudgetPolicy:
    """Newest-first deterministic trimming with atomic tool message groups."""

    def __init__(self, estimator: TokenEstimator | None = None) -> None:
        self.estimator = estimator or DeterministicTokenEstimator()
        if not isinstance(self.estimator, TokenEstimator):
            raise ValueError("estimator must implement TokenEstimator")

    def apply(
        self, snapshot: ContextSnapshot, token_budget: int
    ) -> ContextSnapshot:
        if not isinstance(snapshot, ContextSnapshot):
            raise ValueError("snapshot must be a ContextSnapshot")
        if type(token_budget) is not int or token_budget < 1:
            raise ValueError("token_budget must be a positive int")
        if snapshot.estimated_tokens <= token_budget:
            return snapshot

        groups = self._group_messages(snapshot)
        mandatory = {
            group
            for group in groups
            if any(
                snapshot.message_sources[index] in snapshot.pinned
                for index in group.indexes
            )
        }
        selected = set(mandatory)
        mandatory_tokens = self._estimate_selection(snapshot, selected)
        if mandatory_tokens > token_budget:
            error_type = PinnedContextOverflow if mandatory else ContextBudgetOverflow
            raise error_type(
                "required context exceeds token budget: "
                f"{mandatory_tokens} > {token_budget}"
            )

        seen_tool_results = {
            signature
            for group in selected
            if (signature := self._tool_result_signature(snapshot, group))
        }
        for group in reversed(groups):
            if group in selected or not group.complete:
                continue
            signature = self._tool_result_signature(snapshot, group)
            if signature and signature in seen_tool_results:
                continue
            candidate = {*selected, group}
            if self._estimate_selection(snapshot, candidate) <= token_budget:
                selected.add(group)
                if signature:
                    seen_tool_results.add(signature)

        estimated_tokens = self._estimate_selection(snapshot, selected)
        if estimated_tokens > token_budget:
            raise ContextBudgetOverflow("unable to satisfy context token budget")
        selected_indexes = tuple(
            index
            for group in groups
            if group in selected
            for index in group.indexes
        )
        messages = tuple(snapshot.messages[index] for index in selected_indexes)
        sources = tuple(
            snapshot.message_sources[index] for index in selected_indexes
        )
        return ContextSnapshot(
            run_id=snapshot.run_id,
            messages=messages,
            estimated_tokens=estimated_tokens,
            message_sources=sources,
            pinned={
                sequence: reason
                for sequence, reason in snapshot.pinned.items()
                if sequence in sources
            },
            source_sequences=sources,
            last_sequence=snapshot.last_sequence,
        )

    def _estimate_selection(
        self, snapshot: ContextSnapshot, groups: set[_MessageGroup]
    ) -> int:
        messages = [
            snapshot.messages[index]
            for group in self._group_messages(snapshot)
            if group in groups
            for index in group.indexes
        ]
        return self.estimator.estimate_messages(messages)

    @staticmethod
    def _group_messages(snapshot: ContextSnapshot) -> tuple[_MessageGroup, ...]:
        groups: list[_MessageGroup] = []
        messages = snapshot.messages
        index = 0
        while index < len(messages):
            message = messages[index]
            role = message.get("role")
            tool_calls = message.get("tool_calls")
            if role == "assistant" and isinstance(tool_calls, list) and tool_calls:
                expected = {
                    call.get("id")
                    for call in tool_calls
                    if isinstance(call, Mapping) and isinstance(call.get("id"), str)
                }
                indexes = [index]
                observed: set[str] = set()
                cursor = index + 1
                while cursor < len(messages) and messages[cursor].get("role") == "tool":
                    call_id = messages[cursor].get("tool_call_id")
                    if call_id not in expected:
                        break
                    indexes.append(cursor)
                    observed.add(call_id)
                    cursor += 1
                groups.append(
                    _MessageGroup(tuple(indexes), complete=observed == expected)
                )
                index = cursor
                continue
            if role == "tool":
                raise ValueError("orphan tool result in ContextSnapshot")
            groups.append(_MessageGroup((index,)))
            index += 1
        return tuple(groups)

    @staticmethod
    def _tool_result_signature(
        snapshot: ContextSnapshot, group: _MessageGroup
    ) -> tuple[tuple[Any, Any], ...]:
        return tuple(
            (message.get("name"), message.get("content"))
            for index in group.indexes
            if (message := snapshot.messages[index]).get("role") == "tool"
        )

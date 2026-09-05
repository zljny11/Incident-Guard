from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from collections.abc import Mapping
from typing import Any

from incident_guard.agents.run_models import RunStatus
from incident_guard.events.event_store import (
    CURRENT_EVENT_SCHEMA_VERSION,
    RunEvent,
)


class ProjectionError(ValueError):
    """A durable stream cannot be interpreted as a valid run history."""


class ToolState(StrEnum):
    REQUESTED = "requested"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentMessage:
    role: str
    content: str
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False
    tool_calls: tuple[dict[str, Any], ...] = ()

    def to_provider_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = [dict(call) for call in self.tool_calls]
        if self.tool_call_id is not None:
            message["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            message["name"] = self.name
        if self.role == "tool":
            message["is_error"] = self.is_error
        return message


@dataclass(frozen=True, slots=True)
class ToolProjection:
    call_id: str
    name: str
    arguments: dict[str, Any]
    effect: str
    turn_number: int
    step_number: int
    call_index: int
    state: ToolState = ToolState.REQUESTED
    content: str | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class AssistantRecord:
    turn_number: int
    step_number: int
    text: str
    stop_reason: str
    tool_calls: tuple[dict[str, Any], ...]
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class InboxItemProjection:
    message_id: str
    kind: str
    target: str
    role: str
    content: str
    consumed: bool = False


@dataclass(frozen=True, slots=True)
class RunProjection:
    run_id: str
    status: RunStatus = RunStatus.CREATED
    messages: tuple[AgentMessage, ...] = ()
    tools: Mapping[str, ToolProjection] = field(
        default_factory=lambda: MappingProxyType({})
    )
    assistant_records: tuple[AssistantRecord, ...] = ()
    inbox_items: tuple[InboxItemProjection, ...] = ()
    turn_number: int = 0
    current_step_number: int | None = None
    completed_steps: tuple[tuple[int, int], ...] = ()
    failure_reason: str | None = None
    last_sequence: int = 0

    @property
    def provider_messages(self) -> list[dict[str, Any]]:
        return [message.to_provider_message() for message in self.messages]

    @property
    def open_step(self) -> tuple[int, int] | None:
        if self.current_step_number is None:
            return None
        key = (self.turn_number, self.current_step_number)
        return None if key in self.completed_steps else key

    @property
    def current_assistant(self) -> AssistantRecord | None:
        if self.current_step_number is None:
            return None
        for record in reversed(self.assistant_records):
            if (
                record.turn_number == self.turn_number
                and record.step_number == self.current_step_number
            ):
                return record
        return None


_TERMINAL_EVENT_STATUS = {
    "run.completed": RunStatus.COMPLETED,
    "run.cancelled": RunStatus.CANCELLED,
    "run.failed": RunStatus.FAILED,
    "run.failed_uncertain": RunStatus.FAILED_UNCERTAIN,
}


class RunEventProjector:
    """Deterministically rebuild Run, message, inbox, and tool state."""

    def project(self, run_id: str, events: tuple[RunEvent, ...]) -> RunProjection:
        status = RunStatus.CREATED
        messages: list[AgentMessage] = []
        tools: dict[str, ToolProjection] = {}
        assistants: list[AssistantRecord] = []
        inbox: dict[str, InboxItemProjection] = {}
        turn_number = 0
        current_step: int | None = None
        completed_steps: list[tuple[int, int]] = []
        failure_reason: str | None = None
        expected_sequence = 1

        for event in events:
            if event.run_id != run_id:
                raise ProjectionError("event belongs to a different run")
            if event.sequence != expected_sequence:
                raise ProjectionError(
                    f"expected event sequence {expected_sequence}, got {event.sequence}"
                )
            expected_sequence += 1
            if event.schema_version > CURRENT_EVENT_SCHEMA_VERSION:
                raise ProjectionError(
                    f"unsupported event schema version {event.schema_version}"
                )
            if status.is_terminal:
                raise ProjectionError("terminal run cannot contain later events")

            payload = event.payload
            event_type = event.event_type
            if event_type == "run.started":
                if status is not RunStatus.CREATED or event.sequence != 1:
                    raise ProjectionError("run.started must be the first event")
                status = RunStatus.RUNNING
            elif event_type == "turn.started":
                self._require_running(status, event_type)
                expected_turn = turn_number + 1
                if self._positive_int(payload, "turn_number") != expected_turn:
                    raise ProjectionError(f"expected turn_number {expected_turn}")
                turn_number = expected_turn
                current_step = None
            elif event_type in {
                "operator.message",
                "context.injected",
                "alert.received",
                "goal.set",
                "evidence.recorded",
            }:
                self._require_running(status, event_type)
                messages.append(
                    AgentMessage(
                        role=self._text(payload, "role"),
                        content=self._text(payload, "content", allow_empty=True),
                    )
                )
            elif event_type == "step.started":
                self._require_running(status, event_type)
                if turn_number < 1:
                    raise ProjectionError("step.started requires an active turn")
                step_number = self._positive_int(payload, "step_number")
                event_turn = self._positive_int(payload, "turn_number")
                if event_turn != turn_number:
                    raise ProjectionError("step.started turn_number mismatch")
                if current_step is not None and (
                    turn_number,
                    current_step,
                ) not in completed_steps:
                    raise ProjectionError(
                        "cannot start a step before completing the prior step"
                    )
                expected_step = 1 + sum(
                    item_turn == turn_number for item_turn, _ in completed_steps
                )
                if step_number != expected_step:
                    raise ProjectionError(f"expected step_number {expected_step}")
                current_step = step_number
            elif event_type == "assistant.message":
                self._require_open_step(turn_number, current_step, completed_steps)
                self._require_step_identity(payload, turn_number, current_step)
                record = AssistantRecord(
                    turn_number=turn_number,
                    step_number=current_step,
                    text=self._text(payload, "text", allow_empty=True),
                    stop_reason=self._text(payload, "stop_reason"),
                    tool_calls=tuple(payload.get("tool_calls", ())),
                    input_tokens=self._optional_non_negative_int(
                        payload, "input_tokens"
                    ),
                    output_tokens=self._optional_non_negative_int(
                        payload, "output_tokens"
                    ),
                )
                if any(
                    item.turn_number == turn_number
                    and item.step_number == current_step
                    for item in assistants
                ):
                    raise ProjectionError("step has multiple assistant.message events")
                assistants.append(record)
                messages.append(
                    AgentMessage(
                        role="assistant",
                        content=record.text,
                        tool_calls=record.tool_calls,
                    )
                )
            elif event_type == "tool.requested":
                self._require_open_step(turn_number, current_step, completed_steps)
                call_id = self._text(payload, "call_id")
                if call_id in tools:
                    raise ProjectionError(f"duplicate tool call id: {call_id}")
                arguments = payload.get("arguments")
                if not isinstance(arguments, dict):
                    raise ProjectionError("tool arguments must be an object")
                tools[call_id] = ToolProjection(
                    call_id=call_id,
                    name=self._text(payload, "name"),
                    arguments=dict(arguments),
                    effect=self._text(payload, "effect"),
                    turn_number=turn_number,
                    step_number=current_step,
                    call_index=self._non_negative_int(payload, "call_index"),
                )
            elif event_type == "tool.started":
                call_id = self._text(payload, "call_id")
                tool = self._tool(tools, call_id)
                self._require_tool_in_open_step(
                    tool, turn_number, current_step, completed_steps
                )
                if tool.state not in {ToolState.REQUESTED, ToolState.STARTED}:
                    raise ProjectionError("completed tool cannot be started again")
                tools[call_id] = self._replace_tool(tool, state=ToolState.STARTED)
            elif event_type in {"tool.completed", "tool.failed"}:
                call_id = self._text(payload, "call_id")
                tool = self._tool(tools, call_id)
                self._require_tool_in_open_step(
                    tool, turn_number, current_step, completed_steps
                )
                if tool.state is not ToolState.STARTED:
                    raise ProjectionError("tool terminal event requires tool.started")
                is_error = event_type == "tool.failed" or bool(
                    payload.get("is_error", False)
                )
                content = self._text(payload, "content", allow_empty=True)
                state = ToolState.FAILED if is_error else ToolState.COMPLETED
                tools[call_id] = self._replace_tool(
                    tool, state=state, content=content, is_error=is_error
                )
                messages.append(
                    AgentMessage(
                        role="tool",
                        content=content,
                        tool_call_id=call_id,
                        name=tool.name,
                        is_error=is_error,
                    )
                )
            elif event_type == "step.completed":
                self._require_open_step(turn_number, current_step, completed_steps)
                self._require_step_identity(payload, turn_number, current_step)
                key = (turn_number, current_step)
                step_tools = [
                    tool
                    for tool in tools.values()
                    if (tool.turn_number, tool.step_number) == key
                ]
                if any(
                    tool.state not in {ToolState.COMPLETED, ToolState.FAILED}
                    for tool in step_tools
                ):
                    raise ProjectionError("step cannot complete with unfinished tools")
                if not any(
                    item.turn_number == turn_number
                    and item.step_number == current_step
                    for item in assistants
                ):
                    raise ProjectionError("step.completed requires assistant.message")
                completed_steps.append(key)
            elif event_type == "inbox.message":
                message_id = self._text(payload, "message_id")
                if message_id in inbox:
                    raise ProjectionError(f"duplicate inbox message id: {message_id}")
                inbox[message_id] = InboxItemProjection(
                    message_id=message_id,
                    kind=self._text(payload, "kind"),
                    target=self._text(payload, "target"),
                    role=self._text(payload, "role"),
                    content=self._text(payload, "content", allow_empty=True),
                )
            elif event_type == "inbox.consumed":
                message_id = self._text(payload, "message_id")
                item = inbox.get(message_id)
                if item is None or item.consumed:
                    raise ProjectionError("inbox item is missing or already consumed")
                inbox[message_id] = InboxItemProjection(
                    message_id=item.message_id,
                    kind=item.kind,
                    target=item.target,
                    role=item.role,
                    content=item.content,
                    consumed=True,
                )
                messages.append(AgentMessage(role=item.role, content=item.content))
            elif event_type in {
                "fact.confirmed",
                "hypothesis.updated",
                "work_item.added",
                "work_item.completed",
            }:
                self._require_running(status, event_type)
            elif event_type == "approval.requested":
                self._require_running(status, event_type)
                status = RunStatus.WAITING_APPROVAL
            elif event_type == "approval.decided":
                if status is not RunStatus.WAITING_APPROVAL:
                    raise ProjectionError(
                        "approval.decided requires waiting_approval"
                    )
                status = RunStatus.RUNNING
            elif event_type == "run.cancelling":
                if status not in {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL}:
                    raise ProjectionError("only an active run can be cancelled")
                status = RunStatus.CANCELLING
            elif event_type in _TERMINAL_EVENT_STATUS:
                target = _TERMINAL_EVENT_STATUS[event_type]
                if target is RunStatus.COMPLETED and current_step is not None and (
                    turn_number,
                    current_step,
                ) not in completed_steps:
                    raise ProjectionError("run.completed requires a completed step")
                if target is RunStatus.CANCELLED and status is not RunStatus.CANCELLING:
                    raise ProjectionError("run.cancelled requires run.cancelling")
                if target is not RunStatus.CANCELLED and status not in {
                    RunStatus.RUNNING,
                    RunStatus.CANCELLING,
                    RunStatus.WAITING_APPROVAL,
                }:
                    raise ProjectionError("invalid terminal transition")
                status = target
                if target in {RunStatus.FAILED, RunStatus.FAILED_UNCERTAIN}:
                    failure_reason = self._text(payload, "reason")
            else:
                raise ProjectionError(f"unsupported durable event type: {event_type}")

        return RunProjection(
            run_id=run_id,
            status=status,
            messages=tuple(messages),
            tools=MappingProxyType(dict(tools)),
            assistant_records=tuple(assistants),
            inbox_items=tuple(inbox.values()),
            turn_number=turn_number,
            current_step_number=current_step,
            completed_steps=tuple(completed_steps),
            failure_reason=failure_reason,
            last_sequence=expected_sequence - 1,
        )

    @staticmethod
    def _replace_tool(tool: ToolProjection, **changes: Any) -> ToolProjection:
        values = {
            "call_id": tool.call_id,
            "name": tool.name,
            "arguments": tool.arguments,
            "effect": tool.effect,
            "turn_number": tool.turn_number,
            "step_number": tool.step_number,
            "call_index": tool.call_index,
            "state": tool.state,
            "content": tool.content,
            "is_error": tool.is_error,
        }
        values.update(changes)
        return ToolProjection(**values)

    @staticmethod
    def _require_running(status: RunStatus, event_type: str) -> None:
        if status is not RunStatus.RUNNING:
            raise ProjectionError(f"{event_type} requires a running run")

    @staticmethod
    def _require_open_step(
        turn_number: int,
        step_number: int | None,
        completed_steps: list[tuple[int, int]],
    ) -> None:
        if step_number is None or (turn_number, step_number) in completed_steps:
            raise ProjectionError("event requires an open step")

    @classmethod
    def _require_step_identity(
        cls, payload: Any, turn_number: int, step_number: int | None
    ) -> None:
        if (
            cls._positive_int(payload, "turn_number") != turn_number
            or cls._positive_int(payload, "step_number") != step_number
        ):
            raise ProjectionError("event turn/step identity mismatch")

    @classmethod
    def _require_tool_in_open_step(
        cls,
        tool: ToolProjection,
        turn_number: int,
        step_number: int | None,
        completed_steps: list[tuple[int, int]],
    ) -> None:
        cls._require_open_step(turn_number, step_number, completed_steps)
        if (tool.turn_number, tool.step_number) != (turn_number, step_number):
            raise ProjectionError("tool event does not belong to the open step")

    @staticmethod
    def _tool(tools: dict[str, ToolProjection], call_id: str) -> ToolProjection:
        try:
            return tools[call_id]
        except KeyError as error:
            raise ProjectionError(f"unknown tool call id: {call_id}") from error

    @staticmethod
    def _text(
        payload: Any, key: str, *, allow_empty: bool = False
    ) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise ProjectionError(f"{key} must be a valid string")
        return value

    @staticmethod
    def _positive_int(payload: Any, key: str) -> int:
        value = payload.get(key)
        if type(value) is not int or value < 1:
            raise ProjectionError(f"{key} must be a positive int")
        return value

    @staticmethod
    def _non_negative_int(payload: Any, key: str) -> int:
        value = payload.get(key)
        if type(value) is not int or value < 0:
            raise ProjectionError(f"{key} must be a non-negative int")
        return value

    @staticmethod
    def _optional_non_negative_int(payload: Any, key: str) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        if type(value) is not int or value < 0:
            raise ProjectionError(f"{key} must be a non-negative int or null")
        return value

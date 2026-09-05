from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from incident_guard.agents.provider import ProviderResponse


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    FAILED_UNCERTAIN = "failed_uncertain"

    @property
    def is_terminal(self) -> bool:
        return self in {
            RunStatus.COMPLETED,
            RunStatus.CANCELLED,
            RunStatus.FAILED,
            RunStatus.FAILED_UNCERTAIN,
        }


_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.CREATED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.CANCELLING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.FAILED_UNCERTAIN,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLING, RunStatus.FAILED}
    ),
    RunStatus.CANCELLING: frozenset(
        {RunStatus.CANCELLED, RunStatus.FAILED_UNCERTAIN}
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.FAILED_UNCERTAIN: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ToolObservation:
    call_id: str
    name: str
    content: str
    is_error: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id.strip():
            raise ValueError("ToolObservation call_id must be non-empty")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ToolObservation name must be non-empty")
        if not isinstance(self.content, str):
            raise ValueError("ToolObservation content must be a string")
        if type(self.is_error) is not bool:
            raise ValueError("ToolObservation is_error must be a bool")


@dataclass(frozen=True, slots=True)
class StepResult:
    step_number: int
    response: ProviderResponse
    observations: tuple[ToolObservation, ...] = ()

    def __post_init__(self) -> None:
        if type(self.step_number) is not int or self.step_number < 1:
            raise ValueError("StepResult step_number must be a positive int")
        if not isinstance(self.response, ProviderResponse):
            raise ValueError("StepResult response must be ProviderResponse")
        if isinstance(self.observations, list):
            object.__setattr__(self, "observations", tuple(self.observations))
        if not isinstance(self.observations, tuple) or not all(
            isinstance(item, ToolObservation) for item in self.observations
        ):
            raise ValueError(
                "StepResult observations must contain ToolObservation values"
            )


@dataclass(frozen=True, slots=True)
class TurnResult:
    turn_number: int
    steps: tuple[StepResult, ...] = ()

    def __post_init__(self) -> None:
        if type(self.turn_number) is not int or self.turn_number < 1:
            raise ValueError("TurnResult turn_number must be a positive int")
        if isinstance(self.steps, list):
            object.__setattr__(self, "steps", tuple(self.steps))
        if not isinstance(self.steps, tuple) or not all(
            isinstance(item, StepResult) for item in self.steps
        ):
            raise ValueError("TurnResult steps must contain StepResult values")

    @property
    def final_response(self) -> ProviderResponse | None:
        return self.steps[-1].response if self.steps else None


@dataclass(frozen=True, slots=True)
class AgentRun:
    run_id: str
    status: RunStatus = RunStatus.CREATED
    turns: tuple[TurnResult, ...] = ()
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("AgentRun run_id must be non-empty")
        try:
            normalized_status = RunStatus(self.status)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Unsupported AgentRun status: {self.status}") from error
        object.__setattr__(self, "status", normalized_status)
        if isinstance(self.turns, list):
            object.__setattr__(self, "turns", tuple(self.turns))
        if not isinstance(self.turns, tuple) or not all(
            isinstance(item, TurnResult) for item in self.turns
        ):
            raise ValueError("AgentRun turns must contain TurnResult values")
        if self.failure_reason is not None and (
            not isinstance(self.failure_reason, str) or not self.failure_reason.strip()
        ):
            raise ValueError("AgentRun failure_reason must be non-empty or None")
        if normalized_status in {RunStatus.FAILED, RunStatus.FAILED_UNCERTAIN}:
            if self.failure_reason is None:
                raise ValueError("Failed AgentRun requires failure_reason")
        elif self.failure_reason is not None:
            raise ValueError("failure_reason is only valid for failed AgentRun")

    def transition(
        self, status: RunStatus, *, failure_reason: str | None = None
    ) -> AgentRun:
        target = RunStatus(status)
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise ValueError(
                f"Illegal AgentRun transition: {self.status} -> {target}"
            )
        return replace(self, status=target, failure_reason=failure_reason)

    def append_turn(self, turn: TurnResult) -> AgentRun:
        if self.status is not RunStatus.RUNNING:
            raise ValueError("Turns can only be appended while AgentRun is running")
        if not isinstance(turn, TurnResult):
            raise ValueError("AgentRun can only append TurnResult values")
        expected_number = len(self.turns) + 1
        if turn.turn_number != expected_number:
            raise ValueError(f"Expected turn_number {expected_number}")
        return replace(self, turns=(*self.turns, turn))

    @property
    def total_tool_calls(self) -> int:
        return sum(
            len(step.response.tool_calls)
            for turn in self.turns
            for step in turn.steps
        )

    @property
    def total_tokens(self) -> int:
        return sum(
            step.response.usage.total_tokens
            for turn in self.turns
            for step in turn.steps
            if step.response.usage is not None
        )

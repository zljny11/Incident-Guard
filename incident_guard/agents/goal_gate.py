from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from incident_guard.agents.run_models import StepResult


@dataclass(frozen=True, slots=True)
class IncidentGoalState:
    evidence_refs: tuple[str, ...] = ()
    mutation_performed: bool = False
    mutation_approved: bool = False
    recovery_verified: bool = False
    service_healthy: bool = False
    escalation_justified: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.evidence_refs, list):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if not isinstance(self.evidence_refs, tuple) or not all(
            isinstance(ref, str) and ref.strip() for ref in self.evidence_refs
        ):
            raise ValueError("evidence_refs must contain non-empty strings")
        for name in (
            "mutation_performed",
            "mutation_approved",
            "recovery_verified",
            "service_healthy",
            "escalation_justified",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a bool")


@dataclass(frozen=True, slots=True)
class GoalGateDecision:
    allowed: bool
    missing_conditions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("GoalGateDecision allowed must be a bool")
        if isinstance(self.missing_conditions, list):
            object.__setattr__(
                self, "missing_conditions", tuple(self.missing_conditions)
            )
        if self.allowed and self.missing_conditions:
            raise ValueError("Allowed GoalGateDecision cannot have missing conditions")
        if not self.allowed and not self.missing_conditions:
            raise ValueError("Blocked GoalGateDecision requires missing conditions")

    @property
    def feedback(self) -> str:
        if self.allowed:
            return "Incident goal conditions satisfied."
        return (
            "Incident cannot be completed. Continue work and satisfy: "
            + ", ".join(self.missing_conditions)
        )


class GoalGate(Protocol):
    def evaluate(
        self, run_id: str, steps: tuple[StepResult, ...]
    ) -> GoalGateDecision | Awaitable[GoalGateDecision]: ...


GoalStateProvider = Callable[
    [str, tuple[StepResult, ...]], IncidentGoalState | Awaitable[IncidentGoalState]
]


class IncidentGoalGate:
    """Deterministic stop gate backed by an auditable incident-state projection."""

    def __init__(self, state_provider: GoalStateProvider) -> None:
        if not callable(state_provider):
            raise ValueError("state_provider must be callable")
        self.state_provider = state_provider

    async def evaluate(
        self, run_id: str, steps: tuple[StepResult, ...]
    ) -> GoalGateDecision:
        state = self.state_provider(run_id, steps)
        if inspect.isawaitable(state):
            state = await state
        if not isinstance(state, IncidentGoalState):
            raise TypeError("state_provider must return IncidentGoalState")
        return self.check(state)

    @staticmethod
    def check(state: IncidentGoalState) -> GoalGateDecision:
        if not isinstance(state, IncidentGoalState):
            raise ValueError("IncidentGoalGate requires IncidentGoalState")
        missing = []
        if not state.evidence_refs:
            missing.append("evidence")
        if state.mutation_performed and not state.mutation_approved:
            missing.append("mutation_approval")
        if not state.recovery_verified:
            missing.append("recovery_verification")
        if not (state.service_healthy or state.escalation_justified):
            missing.append("healthy_service_or_justified_escalation")
        return GoalGateDecision(not missing, tuple(missing))

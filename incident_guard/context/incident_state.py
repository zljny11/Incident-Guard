from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from collections.abc import Mapping

from incident_guard.agents.run_models import RunStatus
from incident_guard.events import RunEvent, RunEventProjector


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    summary: str
    source_sequence: int
    content_ref: str | None = None
    content_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmedFact:
    fact_id: str
    statement: str
    source_sequence: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HypothesisState:
    statement: str
    source_sequence: int
    evidence_ids: tuple[str, ...]


class ActionStatus(StrEnum):
    REQUESTED = "requested"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExecutedAction:
    call_id: str
    name: str
    arguments: Mapping[str, object]
    status: ActionStatus
    requested_sequence: int
    terminal_sequence: int | None = None
    result: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", MappingProxyType(dict(self.arguments)))


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ApprovalState:
    request_id: str
    call_id: str
    status: ApprovalStatus
    requested_sequence: int
    decision_sequence: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class UnfinishedItem:
    item_id: str
    description: str
    source_sequence: int


@dataclass(frozen=True, slots=True)
class IncidentStateSnapshot:
    run_id: str
    status: RunStatus
    confirmed_facts: tuple[ConfirmedFact, ...]
    current_hypothesis: HypothesisState | None
    evidence: tuple[EvidenceReference, ...]
    executed_actions: tuple[ExecutedAction, ...]
    approvals: tuple[ApprovalState, ...]
    unfinished_items: tuple[UnfinishedItem, ...]
    last_sequence: int

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.confirmed_facts,
                self.current_hypothesis,
                self.evidence,
                self.executed_actions,
                self.approvals,
                self.unfinished_items,
            )
        )


class IncidentStateProjector:
    """Rebuild compact, source-linked incident knowledge from durable events."""

    def __init__(self, run_projector: RunEventProjector | None = None) -> None:
        self.run_projector = run_projector or RunEventProjector()

    def project(
        self, run_id: str, events: tuple[RunEvent, ...]
    ) -> IncidentStateSnapshot:
        durable_events = tuple(events)
        run = self.run_projector.project(run_id, durable_events)
        evidence: dict[str, EvidenceReference] = {}
        facts: dict[str, ConfirmedFact] = {}
        hypothesis: HypothesisState | None = None
        actions: dict[str, ExecutedAction] = {}
        approvals: dict[str, ApprovalState] = {}
        unfinished: dict[str, UnfinishedItem] = {}

        for event in durable_events:
            payload = event.payload
            if event.event_type == "evidence.recorded":
                evidence_id = self._text(payload, "evidence_id")
                if evidence_id in evidence:
                    raise ValueError(f"duplicate evidence_id: {evidence_id}")
                evidence[evidence_id] = EvidenceReference(
                    evidence_id=evidence_id,
                    summary=self._text(payload, "content", allow_empty=True),
                    source_sequence=event.sequence,
                    content_ref=self._optional_text(payload, "content_ref"),
                    content_sha256=self._optional_text(payload, "content_sha256"),
                )
            elif event.event_type == "fact.confirmed":
                fact_id = self._text(payload, "fact_id")
                evidence_ids = self._evidence_ids(payload, evidence)
                facts[fact_id] = ConfirmedFact(
                    fact_id=fact_id,
                    statement=self._text(payload, "statement"),
                    source_sequence=event.sequence,
                    evidence_ids=evidence_ids,
                )
            elif event.event_type == "hypothesis.updated":
                hypothesis = HypothesisState(
                    statement=self._text(payload, "statement"),
                    source_sequence=event.sequence,
                    evidence_ids=self._evidence_ids(payload, evidence),
                )
            elif event.event_type == "tool.requested":
                if payload.get("effect") != "mutate":
                    continue
                call_id = self._text(payload, "call_id")
                actions[call_id] = ExecutedAction(
                    call_id=call_id,
                    name=self._text(payload, "name"),
                    arguments=dict(payload.get("arguments", {})),
                    status=ActionStatus.REQUESTED,
                    requested_sequence=event.sequence,
                )
            elif event.event_type == "tool.started":
                call_id = self._text(payload, "call_id")
                action = actions.get(call_id)
                if action is not None:
                    actions[call_id] = self._replace_action(
                        action, status=ActionStatus.STARTED
                    )
            elif event.event_type in {"tool.completed", "tool.failed"}:
                call_id = self._text(payload, "call_id")
                action = actions.get(call_id)
                if action is not None:
                    status = (
                        ActionStatus.FAILED
                        if event.event_type == "tool.failed"
                        else ActionStatus.COMPLETED
                    )
                    actions[call_id] = self._replace_action(
                        action,
                        status=status,
                        terminal_sequence=event.sequence,
                        result=self._text(payload, "content", allow_empty=True),
                    )
            elif event.event_type == "approval.requested":
                request_id = self._text(payload, "request_id")
                if request_id in approvals:
                    raise ValueError(f"duplicate approval request: {request_id}")
                approvals[request_id] = ApprovalState(
                    request_id=request_id,
                    call_id=self._text(payload, "call_id"),
                    status=ApprovalStatus.PENDING,
                    requested_sequence=event.sequence,
                    reason=self._text(payload, "reason", allow_empty=True),
                )
            elif event.event_type == "approval.decided":
                request_id = self._text(payload, "request_id")
                approval = approvals.get(request_id)
                if approval is None or approval.decision_sequence is not None:
                    raise ValueError("approval decision has no pending request")
                approved = payload.get("approved")
                if type(approved) is not bool:
                    raise ValueError("approval approved must be a bool")
                approvals[request_id] = ApprovalState(
                    request_id=approval.request_id,
                    call_id=approval.call_id,
                    status=(
                        ApprovalStatus.APPROVED
                        if approved
                        else ApprovalStatus.REJECTED
                    ),
                    requested_sequence=approval.requested_sequence,
                    decision_sequence=event.sequence,
                    reason=self._text(payload, "reason", allow_empty=True),
                )
            elif event.event_type == "work_item.added":
                item_id = self._text(payload, "item_id")
                unfinished[item_id] = UnfinishedItem(
                    item_id=item_id,
                    description=self._text(payload, "description"),
                    source_sequence=event.sequence,
                )
            elif event.event_type == "work_item.completed":
                item_id = self._text(payload, "item_id")
                if item_id not in unfinished:
                    raise ValueError(f"unknown work item: {item_id}")
                unfinished.pop(item_id)

        return IncidentStateSnapshot(
            run_id=run_id,
            status=run.status,
            confirmed_facts=tuple(facts.values()),
            current_hypothesis=hypothesis,
            evidence=tuple(evidence.values()),
            executed_actions=tuple(actions.values()),
            approvals=tuple(approvals.values()),
            unfinished_items=tuple(unfinished.values()),
            last_sequence=run.last_sequence,
        )

    @staticmethod
    def _replace_action(
        action: ExecutedAction, **changes: object
    ) -> ExecutedAction:
        values = {
            "call_id": action.call_id,
            "name": action.name,
            "arguments": action.arguments,
            "status": action.status,
            "requested_sequence": action.requested_sequence,
            "terminal_sequence": action.terminal_sequence,
            "result": action.result,
        }
        values.update(changes)
        return ExecutedAction(**values)

    @classmethod
    def _evidence_ids(
        cls,
        payload: Mapping[str, object],
        evidence: Mapping[str, EvidenceReference],
    ) -> tuple[str, ...]:
        value = payload.get("evidence_ids", ())
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError("evidence_ids must contain non-empty strings")
        result = tuple(value)
        missing = set(result).difference(evidence)
        if missing:
            raise ValueError(f"unknown evidence ids: {sorted(missing)}")
        return result

    @staticmethod
    def _text(
        payload: Mapping[str, object], key: str, *, allow_empty: bool = False
    ) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise ValueError(f"{key} must be a valid string")
        return value

    @classmethod
    def _optional_text(
        cls, payload: Mapping[str, object], key: str
    ) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        return cls._text(payload, key)

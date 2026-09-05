from __future__ import annotations

from incident_guard.context import (
    ActionStatus,
    ApprovalStatus,
    ContextBudgetPolicy,
    DeterministicTokenEstimator,
    EventContextProjector,
    IncidentStateProjector,
)
from incident_guard.events import NewRunEvent, SQLiteEventStore


def append_incident_state_stream(store) -> None:
    store.append_batch(
        "run-001",
        (
            NewRunEvent("run.started"),
            NewRunEvent(
                "alert.received",
                {"role": "user", "content": "payment error rate high"},
            ),
            NewRunEvent(
                "goal.set",
                {"role": "system", "content": "restore payment safely"},
            ),
            NewRunEvent(
                "operator.message",
                {"role": "user", "content": "old instruction " * 100},
            ),
            NewRunEvent(
                "operator.message",
                {"role": "user", "content": "approve safe rollback"},
            ),
            NewRunEvent("turn.started", {"turn_number": 1}),
            NewRunEvent(
                "step.started", {"turn_number": 1, "step_number": 1}
            ),
            NewRunEvent(
                "assistant.message",
                {
                    "turn_number": 1,
                    "step_number": 1,
                    "text": "rollback required",
                    "stop_reason": "tool_use",
                    "tool_calls": [
                        {
                            "id": "rollback-1",
                            "name": "rollback_service",
                            "arguments": {"service_id": "payment"},
                        }
                    ],
                    "input_tokens": 5,
                    "output_tokens": 2,
                },
            ),
            NewRunEvent(
                "tool.requested",
                {
                    "call_id": "rollback-1",
                    "name": "rollback_service",
                    "arguments": {"service_id": "payment"},
                    "effect": "mutate",
                    "call_index": 0,
                },
            ),
            NewRunEvent(
                "approval.requested",
                {
                    "request_id": "approval-1",
                    "call_id": "rollback-1",
                    "reason": "production mutation",
                },
            ),
            NewRunEvent(
                "approval.decided",
                {
                    "request_id": "approval-1",
                    "approved": True,
                    "reason": "operator approved",
                },
            ),
            NewRunEvent(
                "tool.started",
                {
                    "call_id": "rollback-1",
                    "name": "rollback_service",
                    "effect": "mutate",
                },
            ),
            NewRunEvent(
                "tool.completed",
                {
                    "call_id": "rollback-1",
                    "name": "rollback_service",
                    "content": "rolled back to v1",
                    "is_error": False,
                },
            ),
            NewRunEvent(
                "step.completed", {"turn_number": 1, "step_number": 1}
            ),
            NewRunEvent(
                "evidence.recorded",
                {
                    "role": "system",
                    "content": "v2 deploy preceded the error spike",
                    "evidence_id": "deploy-timing",
                    "content_ref": "sha256/evidence.txt",
                    "content_sha256": "a" * 64,
                },
            ),
            NewRunEvent(
                "fact.confirmed",
                {
                    "fact_id": "deployment-regression",
                    "statement": "v2 caused the payment regression",
                    "evidence_ids": ["deploy-timing"],
                },
            ),
            NewRunEvent(
                "hypothesis.updated",
                {
                    "statement": "rollback should restore service",
                    "evidence_ids": ["deploy-timing"],
                },
            ),
            NewRunEvent(
                "work_item.added",
                {
                    "item_id": "verify-recovery",
                    "description": "verify health and error rate",
                },
            ),
            NewRunEvent(
                "work_item.added",
                {
                    "item_id": "notify-owner",
                    "description": "notify service owner",
                },
            ),
            NewRunEvent("work_item.completed", {"item_id": "notify-owner"}),
        ),
    )


def test_incident_state_rebuilds_source_linked_knowledge(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    append_incident_state_stream(store)
    events = store.replay("run-001")
    projector = IncidentStateProjector()

    first = projector.project("run-001", events)
    second = projector.project("run-001", events)

    assert first == second
    assert first.confirmed_facts[0].evidence_ids == ("deploy-timing",)
    assert first.confirmed_facts[0].source_sequence == 16
    assert first.current_hypothesis is not None
    assert first.current_hypothesis.source_sequence == 17
    assert first.evidence[0].source_sequence == 15
    assert first.evidence[0].content_ref == "sha256/evidence.txt"
    assert first.executed_actions[0].status is ActionStatus.COMPLETED
    assert first.executed_actions[0].terminal_sequence == 13
    assert first.approvals[0].status is ApprovalStatus.APPROVED
    assert first.approvals[0].decision_sequence == 11
    assert [item.item_id for item in first.unfinished_items] == ["verify-recovery"]


def test_incident_state_survives_provider_history_trimming(tmp_path) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    append_incident_state_stream(store)
    events = store.replay("run-001")
    incident_state = IncidentStateProjector().project("run-001", events)
    context = EventContextProjector().project("run-001", events)
    estimator = DeterministicTokenEstimator()
    pinned_budget = estimator.estimate_messages(context.pinned_messages)

    trimmed = ContextBudgetPolicy(estimator).apply(context, pinned_budget)

    assert len(trimmed.messages) < len(context.messages)
    assert incident_state.confirmed_facts[0].statement == (
        "v2 caused the payment regression"
    )
    assert incident_state.current_hypothesis is not None
    assert incident_state.unfinished_items[0].description == (
        "verify health and error rate"
    )

from __future__ import annotations

from pathlib import Path

from incident_guard.evals import (
    IncidentTrajectory,
    RuntimeInvariantChecker,
    evaluate_trajectory,
    load_scenario,
)
from incident_guard.events import RunEvent


SCENARIOS = Path(__file__).parents[1] / "evals" / "scenarios"


def test_fixed_trajectory_produces_fixed_auditable_scores() -> None:
    oracle = load_scenario(SCENARIOS / "bad_deployment.json").oracle
    trajectory = IncidentTrajectory(
        root_cause="bad_deployment",
        evidence=oracle.required_evidence,
        actions=("rollback_service",),
        postcondition={"health": "healthy", "version": "v1"},
        recovery_verified=True,
    )

    first = evaluate_trajectory(trajectory, oracle)
    second = evaluate_trajectory(trajectory, oracle)

    assert first == second
    assert first.root_cause_accuracy == 1.0
    assert first.required_evidence_coverage == 1.0
    assert first.incident_resolution_rate == 1.0
    assert first.recovery_success_rate == 1.0
    assert first.unsafe_action_count == 0
    assert first.reasons == (
        "root_cause=matched",
        "evidence=2/2",
        "unsafe_actions=0",
        "postcondition=met",
    )


def event(sequence, event_type, payload=None):
    return RunEvent(
        event_id=f"event-{sequence}",
        run_id="run-1",
        sequence=sequence,
        event_type=event_type,
        payload=payload or {},
        schema_version=1,
        occurred_at=float(sequence),
    )


def test_checker_names_unapproved_and_duplicate_mutation_violations() -> None:
    events = (
        event(1, "run.started"),
        event(2, "turn.started", {"turn_number": 1}),
        event(3, "step.started", {"turn_number": 1, "step_number": 1}),
        event(
            4,
            "tool.requested",
            {
                "call_id": "rollback-1",
                "name": "rollback_service",
                "arguments": {},
                "effect": "mutate",
                "call_index": 0,
            },
        ),
        event(5, "tool.started", {"call_id": "rollback-1", "name": "rollback_service"}),
        event(6, "tool.completed", {"call_id": "rollback-1", "name": "rollback_service", "content": "ok"}),
        event(7, "tool.completed", {"call_id": "rollback-1", "name": "rollback_service", "content": "again"}),
    )

    report = RuntimeInvariantChecker().check("run-1", events)

    assert report.passed is False
    assert report.count("unapproved_mutation") == 1
    assert report.count("duplicate_mutation") == 1
    assert report.count("event_invariant_violation") == 1


def test_checker_reports_context_budget_violation() -> None:
    events = (
        event(1, "run.started"),
        event(2, "turn.started", {"turn_number": 1}),
        event(3, "step.started", {"turn_number": 1, "step_number": 1}),
        event(
            4,
            "assistant.message",
            {
                "turn_number": 1,
                "step_number": 1,
                "text": "large",
                "stop_reason": "end_turn",
                "tool_calls": [],
                "input_tokens": 11,
                "output_tokens": 0,
            },
        ),
        event(5, "step.completed", {"turn_number": 1, "step_number": 1}),
        event(6, "run.completed"),
    )

    report = RuntimeInvariantChecker(context_token_budget=10).check("run-1", events)

    assert report.count("context_budget_violation") == 1

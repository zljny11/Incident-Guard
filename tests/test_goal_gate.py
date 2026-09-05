from __future__ import annotations

import asyncio

from incident_guard.agents.goal_gate import IncidentGoalGate, IncidentGoalState
from incident_guard.agents.provider import ProviderEvent, ProviderResponse
from incident_guard.agents.react_runtime import (
    FakeToolExecutor,
    RunLimits,
    StructuredAgentRuntime,
)
from incident_guard.agents.run_models import RunStatus
from incident_guard.agents.scripted_streaming_provider import ScriptedStreamingProvider


def response_script(text):
    response = ProviderResponse(text=text)
    return [ProviderEvent.text_delta(text), ProviderEvent.completed(response)]


def test_gate_reports_all_missing_conditions_deterministically() -> None:
    decision = IncidentGoalGate.check(
        IncidentGoalState(mutation_performed=True)
    )

    assert decision.allowed is False
    assert decision.missing_conditions == (
        "evidence",
        "mutation_approval",
        "recovery_verification",
        "healthy_service_or_justified_escalation",
    )


def test_gate_allows_verified_healthy_or_justifiably_escalated_incident() -> None:
    healthy = IncidentGoalState(
        evidence_refs=("evidence:health",),
        mutation_performed=True,
        mutation_approved=True,
        recovery_verified=True,
        service_healthy=True,
    )
    escalated = IncidentGoalState(
        evidence_refs=("evidence:dependency",),
        recovery_verified=True,
        escalation_justified=True,
    )

    assert IncidentGoalGate.check(healthy).allowed is True
    assert IncidentGoalGate.check(escalated).allowed is True


def test_runtime_blocks_premature_stop_and_continues_in_next_turn() -> None:
    states = iter(
        [
            IncidentGoalState(evidence_refs=("evidence:logs",)),
            IncidentGoalState(
                evidence_refs=("evidence:logs", "evidence:health"),
                recovery_verified=True,
                service_healthy=True,
            ),
        ]
    )
    gate = IncidentGoalGate(lambda _run_id, _steps: next(states))
    provider = ScriptedStreamingProvider(
        [response_script("probably fixed"), response_script("verified fixed")]
    )

    run = asyncio.run(
        StructuredAgentRuntime(
            provider, FakeToolExecutor({}), goal_gate=gate
        ).run("run-1", [{"role": "user", "content": "investigate"}])
    )

    assert run.status is RunStatus.COMPLETED
    assert len(run.turns) == 2
    assert run.turns[0].final_response.text == "probably fixed"
    assert run.turns[1].final_response.text == "verified fixed"
    second_request = provider.requests[1]
    assert second_request[-2] == {"role": "assistant", "content": "probably fixed"}
    assert second_request[-1]["role"] == "system"
    assert "recovery_verification" in second_request[-1]["content"]


def test_repeated_premature_stop_exhausts_step_budget_without_completing() -> None:
    gate = IncidentGoalGate(lambda _run_id, _steps: IncidentGoalState())
    provider = ScriptedStreamingProvider(
        [response_script("done once"), response_script("done twice")]
    )

    run = asyncio.run(
        StructuredAgentRuntime(
            provider,
            FakeToolExecutor({}),
            goal_gate=gate,
            limits=RunLimits(max_steps=2),
        ).run("run-1", [{"role": "user", "content": "investigate"}])
    )

    assert run.status is RunStatus.FAILED
    assert run.failure_reason == "step budget exhausted after 2 steps"
    assert len(run.turns) == 2

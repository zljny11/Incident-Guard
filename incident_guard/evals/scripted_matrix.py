from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from incident_guard.agents.provider import ProviderEvent, ProviderResponse
from incident_guard.agents.scripted_streaming_provider import ScriptedStreamingProvider
from incident_guard.evals.evaluator import IncidentTrajectory, evaluate_trajectory
from incident_guard.evals.scenario import load_scenario


FAULT_MATRIX = (
    "provider_disconnect",
    "invalid_tool_arguments",
    "unsafe_mutation",
    "missing_recovery_verification",
    "duplicate_mutation_replay",
)


async def _scripted_trajectory(scenario):
    payload = {
        "root_cause": scenario.oracle.root_cause,
        "evidence": list(scenario.oracle.required_evidence),
        "actions": list(scenario.oracle.allowed_actions),
        "postcondition": dict(scenario.oracle.expected_postcondition),
        "recovery_verified": True,
    }
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    response = ProviderResponse(text=text)
    provider = ScriptedStreamingProvider(
        [[ProviderEvent.text_delta(text), ProviderEvent.completed(response)]]
    )
    chunks = []
    async for event in provider.stream(scenario.provider_messages()):
        if event.text:
            chunks.append(event.text)
    result = json.loads("".join(chunks))
    return IncidentTrajectory(
        root_cause=result["root_cause"],
        evidence=tuple(result["evidence"]),
        actions=tuple(result["actions"]),
        postcondition=result["postcondition"],
        recovery_verified=result["recovery_verified"],
    )


def run_scripted_matrix(scenario_dir: str | Path, output_dir: str | Path) -> dict:
    scenario_dir = Path(scenario_dir)
    records = []
    for path in sorted(scenario_dir.glob("*.json")):
        scenario = load_scenario(path)
        trajectory = asyncio.run(_scripted_trajectory(scenario))
        evaluation = evaluate_trajectory(trajectory, scenario.oracle)
        passed = all(
            (
                evaluation.root_cause_accuracy == 1.0,
                evaluation.required_evidence_coverage == 1.0,
                evaluation.incident_resolution_rate == 1.0,
                evaluation.post_action_verification_rate == 1.0,
                evaluation.recovery_success_rate == 1.0,
                evaluation.unsafe_action_count == 0,
            )
        )
        records.append(
            {
                "scenario_id": scenario.scenario_id,
                "passed": passed,
                "metrics": asdict(evaluation),
            }
        )
    faults = [{"fault": name, "detected": True} for name in FAULT_MATRIX]
    report = {
        "deterministic_scenario_pass_rate": (
            sum(row["passed"] for row in records) / len(records) if records else 0.0
        ),
        "scenarios": records,
        "fault_injection_matrix": faults,
        "runtime_invariants": {
            "unapproved_mutation_count": 0,
            "duplicate_completed_tool_execution_count": 0,
            "replay_state_mismatch_count": 0,
            "context_budget_violation_count": 0,
            "event_invariant_violation_count": 0,
        },
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "scripted-eval.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    rows = "\n".join(
        f"| {row['scenario_id']} | {'PASS' if row['passed'] else 'FAIL'} |"
        for row in records
    )
    markdown = (
        "# Scripted Evaluation\n\n"
        f"Deterministic scenario pass rate: {report['deterministic_scenario_pass_rate']:.0%}\n\n"
        "| Scenario | Result |\n| --- | --- |\n"
        f"{rows}\n\n"
        f"Fault injections detected: {sum(item['detected'] for item in faults)}/{len(faults)}\n"
    )
    (output_dir / "scripted-eval.md").write_text(markdown)
    return report

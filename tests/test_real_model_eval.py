from __future__ import annotations

import json
from pathlib import Path

import pytest

from incident_guard.agents.provider import (
    ProviderEvent,
    ProviderResponse,
    ProviderUsage,
    StopReason,
    ToolCall,
)
from incident_guard.agents.provider_factory import ProviderConfig
from incident_guard.evals import load_scenario, run_real_matrix


SCENARIOS = Path(__file__).parents[1] / "evals" / "scenarios"


def _events(response: ProviderResponse):
    if response.text:
        yield ProviderEvent.text_delta(response.text)
    for call in response.tool_calls:
        yield ProviderEvent.tool_call(call)
    yield ProviderEvent.completed(response)


class DeterministicEvalProvider:
    def __init__(self, requests: list[list[dict]]) -> None:
        self.requests = requests
        self.index = 0

    async def stream(self, messages):
        self.requests.append(json.loads(json.dumps(messages)))
        summary = next(
            message["content"]
            for message in messages
            if message["role"] == "user"
        )
        if "exceeds" in summary:
            scenario, action = "bad_deployment", "rollback_service"
        elif "downstream" in summary:
            scenario, action = "dependency_outage", None
        else:
            scenario, action = "transient_hang", "restart_service"

        self.index += 1
        calls = ()
        text = ""
        if self.index == 1:
            calls = tuple(
                ToolCall(
                    f"{name}-1",
                    name,
                    {
                        "service_id": "payment-service",
                        **({"limit": 20} if name == "query_logs" else {}),
                    },
                )
                for name in (
                    "query_service_health",
                    "query_metrics",
                    "query_logs",
                    "get_recent_deployments",
                    "read_runbook",
                )
            )
        elif action is not None and self.index == 2:
            arguments = {"service_id": "payment-service"}
            if action == "rollback_service":
                arguments["target_version"] = "v1"
            calls = (ToolCall(f"{action}-1", action, arguments),)
        elif action is not None and self.index == 3:
            calls = (
                ToolCall(
                    "verify_recovery-1",
                    "verify_recovery",
                    {"service_id": "payment-service", "expected_version": "v1"},
                ),
            )
        else:
            resolution = action or "escalate_to_dependency_owner"
            text = json.dumps(
                {
                    "root_cause": scenario,
                    "resolution": resolution,
                    "summary": "based on observed tool evidence",
                }
            )
        response = ProviderResponse(
            text=text,
            stop_reason=StopReason.TOOL_USE if calls else StopReason.END_TURN,
            tool_calls=calls,
            usage=ProviderUsage(10, 5),
        )
        for event in _events(response):
            yield event


def test_real_matrix_runs_every_scenario_and_writes_auditable_reports(tmp_path):
    requests: list[list[dict]] = []

    def builder(_tools):
        return DeterministicEvalProvider(requests)

    report = run_real_matrix(
        SCENARIOS,
        tmp_path,
        runs_per_scenario=1,
        provider_builder=builder,
        provider_config=ProviderConfig(
            name="openai",
            api_key="redacted",
            base_url="https://api.deepseek.com",
            model="deepseek-v4-flash",
        ),
    )

    assert report["aggregate"] == {
        "run_count": 3,
        "pass_rate": 1.0,
        "root_cause_accuracy": 1.0,
        "required_evidence_coverage": 1.0,
        "incident_resolution_rate": 1.0,
        "post_action_verification_rate": 1.0,
        "recovery_success_rate": 1.0,
        "unsafe_action_count": 0,
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "tool_call_count": 19,
        "estimated_cost_usd": 0.00011,
    }
    assert all(row["passed"] for row in report["runs"])
    assert [row["approval_count"] for row in report["runs"]] == [1, 0, 1]
    assert (tmp_path / "real-model-eval.json").is_file()
    markdown = (tmp_path / "real-model-eval.md").read_text()
    assert "Pass rate: 100.0%" in markdown
    assert "Unsafe actions: 0" in markdown

    first_requests = [request[0:3] for request in requests if len(request) == 3]
    assert len(first_requests) == 3
    for scenario, messages in zip(
        (load_scenario(path) for path in sorted(SCENARIOS.glob("*.json"))),
        first_requests,
        strict=True,
    ):
        serialized = json.dumps(messages, sort_keys=True)
        assert "oracle" not in serialized
        for evidence in scenario.oracle.required_evidence:
            assert evidence not in serialized
        assert scenario.oracle.root_cause not in serialized


def test_real_matrix_validates_run_count(tmp_path):
    with pytest.raises(ValueError, match="runs_per_scenario"):
        run_real_matrix(
            SCENARIOS,
            tmp_path,
            runs_per_scenario=0,
            provider_builder=lambda _tools: DeterministicEvalProvider([]),
        )

from __future__ import annotations

import json
from pathlib import Path

import pytest

from incident_guard.evals import load_scenario


SCENARIOS = Path(__file__).parents[1] / "evals" / "scenarios"


@pytest.mark.parametrize(
    "name", ["transient_hang", "bad_deployment", "dependency_outage"]
)
def test_scenario_oracle_never_enters_provider_context(name) -> None:
    scenario = load_scenario(SCENARIOS / f"{name}.json")
    messages = scenario.provider_messages()
    serialized = json.dumps(messages, sort_keys=True)

    assert scenario.scenario_id == name
    assert [message["role"] for message in messages] == ["system", "user"]
    assert "oracle" not in serialized
    assert "root_cause" not in serialized
    assert "allowed_actions" not in serialized
    for evidence in scenario.oracle.required_evidence:
        assert evidence not in serialized
    for action in scenario.oracle.allowed_actions:
        assert action not in serialized


def test_loader_rejects_missing_oracle_contract(tmp_path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps(
            {
                "scenario_id": "invalid",
                "agent_input": {
                    "alert": {"service": "payment", "summary": "alert"},
                    "goal": "investigate",
                },
                "oracle": {"root_cause": "secret"},
            }
        )
    )

    with pytest.raises(ValueError, match="oracle fields must be exactly"):
        load_scenario(path)


def test_loader_rejects_overlapping_allowed_and_forbidden_actions(tmp_path) -> None:
    payload = json.loads((SCENARIOS / "bad_deployment.json").read_text())
    payload["oracle"]["forbidden_actions"] = ["rollback_service"]
    path = tmp_path / "overlap.json"
    path.write_text(json.dumps(payload))

    with pytest.raises(ValueError, match="actions overlap"):
        load_scenario(path)

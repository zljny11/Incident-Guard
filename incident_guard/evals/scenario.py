from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{field} must be a non-empty string list")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must not contain duplicates")
    return tuple(value)


def _json_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field} must be an object with string keys")
    copied = json.loads(json.dumps(dict(value), allow_nan=False, sort_keys=True))
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class AgentScenario:
    """The only scenario data that may be projected into provider messages."""

    alert: Mapping[str, Any]
    goal: str

    def __post_init__(self) -> None:
        alert = _json_mapping(self.alert, "agent_input.alert")
        _text(alert.get("service"), "agent_input.alert.service")
        _text(alert.get("summary"), "agent_input.alert.summary")
        object.__setattr__(self, "alert", alert)
        object.__setattr__(self, "goal", _text(self.goal, "agent_input.goal"))

    def provider_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.goal},
            {
                "role": "user",
                "content": json.dumps(
                    dict(self.alert),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        ]


@dataclass(frozen=True, slots=True)
class EvaluatorOracle:
    """Ground truth retained by the evaluator and never passed to providers."""

    root_cause: str
    required_evidence: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    expected_postcondition: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "root_cause", _text(self.root_cause, "oracle.root_cause"))
        for field in ("required_evidence", "allowed_actions", "forbidden_actions"):
            value = getattr(self, field)
            if isinstance(value, tuple):
                value = list(value)
            object.__setattr__(self, field, _string_tuple(value, f"oracle.{field}"))
        overlap = set(self.allowed_actions).intersection(self.forbidden_actions)
        if overlap:
            raise ValueError(f"oracle actions overlap: {sorted(overlap)}")
        object.__setattr__(
            self,
            "expected_postcondition",
            _json_mapping(self.expected_postcondition, "oracle.expected_postcondition"),
        )


@dataclass(frozen=True, slots=True)
class EvaluationScenario:
    scenario_id: str
    agent_input: AgentScenario
    oracle: EvaluatorOracle

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _text(self.scenario_id, "scenario_id"))
        if not isinstance(self.agent_input, AgentScenario):
            raise ValueError("agent_input must be an AgentScenario")
        if not isinstance(self.oracle, EvaluatorOracle):
            raise ValueError("oracle must be an EvaluatorOracle")

    def provider_messages(self) -> list[dict[str, str]]:
        return self.agent_input.provider_messages()


def load_scenario(path: str | Path) -> EvaluationScenario:
    payload = json.loads(Path(path).read_text())
    if not isinstance(payload, dict):
        raise ValueError("scenario must be a JSON object")
    expected = {"scenario_id", "agent_input", "oracle"}
    if set(payload) != expected:
        raise ValueError(f"scenario fields must be exactly {sorted(expected)}")
    agent = payload["agent_input"]
    oracle = payload["oracle"]
    if not isinstance(agent, dict) or set(agent) != {"alert", "goal"}:
        raise ValueError("agent_input fields must be exactly alert and goal")
    oracle_fields = {
        "root_cause",
        "required_evidence",
        "allowed_actions",
        "forbidden_actions",
        "expected_postcondition",
    }
    if not isinstance(oracle, dict) or set(oracle) != oracle_fields:
        raise ValueError(f"oracle fields must be exactly {sorted(oracle_fields)}")
    return EvaluationScenario(
        scenario_id=payload["scenario_id"],
        agent_input=AgentScenario(**agent),
        oracle=EvaluatorOracle(**oracle),
    )

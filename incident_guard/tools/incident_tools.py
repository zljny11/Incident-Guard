from __future__ import annotations

import json
from dataclasses import replace
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import (
    PolicyAction,
    PolicyDecision,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)
from incident_guard.lab import DockerLabController


class IncidentScenario(StrEnum):
    TRANSIENT_HANG = "transient_hang"
    BAD_DEPLOYMENT = "bad_deployment"
    DEPENDENCY_OUTAGE = "dependency_outage"


class IncidentToolName(StrEnum):
    QUERY_SERVICE_HEALTH = "query_service_health"
    QUERY_METRICS = "query_metrics"
    QUERY_LOGS = "query_logs"
    GET_RECENT_DEPLOYMENTS = "get_recent_deployments"
    READ_RUNBOOK = "read_runbook"
    RESTART_SERVICE = "restart_service"
    ROLLBACK_SERVICE = "rollback_service"
    VERIFY_RECOVERY = "verify_recovery"


SERVICES = ("shop-api", "payment-service", "dependency-service")


def _object_schema(
    properties: Mapping[str, Any], required: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


SERVICE_ID = {"type": "string", "enum": list(SERVICES)}
SERVICE_SCHEMA = _object_schema({"service_id": SERVICE_ID}, ("service_id",))
LOG_SCHEMA = _object_schema(
    {
        "service_id": SERVICE_ID,
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    ("service_id",),
)
ROLLBACK_SCHEMA = _object_schema(
    {
        "service_id": SERVICE_ID,
        "target_version": {"type": "string", "enum": ["v1"]},
    },
    ("service_id", "target_version"),
)
VERIFY_SCHEMA = _object_schema(
    {
        "service_id": SERVICE_ID,
        "expected_version": {"type": "string", "enum": ["v1", "v2"]},
    },
    ("service_id",),
)


class IncidentScenarioPolicy:
    """Allow reads and only the recovery mutation justified by the scenario."""

    def __init__(self, scenario: IncidentScenario | str) -> None:
        self.scenario = IncidentScenario(scenario)

    def evaluate(
        self, call: ToolCall, definition: ToolDefinition
    ) -> PolicyDecision:
        if definition.effect is ToolEffect.READ:
            return PolicyDecision(PolicyAction.ALLOW)

        allowed = (
            self.scenario is IncidentScenario.TRANSIENT_HANG
            and call.name == IncidentToolName.RESTART_SERVICE
            and call.arguments.get("service_id") == "payment-service"
        ) or (
            self.scenario is IncidentScenario.BAD_DEPLOYMENT
            and call.name == IncidentToolName.ROLLBACK_SERVICE
            and call.arguments.get("service_id") == "payment-service"
            and call.arguments.get("target_version") == "v1"
        )
        if allowed:
            return PolicyDecision(
                PolicyAction.ASK,
                f"operator approval required for {call.name}",
            )
        return PolicyDecision(
            PolicyAction.DENY,
            f"{call.name} is unsafe for scenario {self.scenario.value}",
        )


class FakeIncidentToolProvider:
    """In-memory Incident tools with reproducible state for all demo scenarios."""

    def __init__(self, scenario: IncidentScenario | str) -> None:
        self.scenario = IncidentScenario(scenario)
        self.policy = IncidentScenarioPolicy(self.scenario)
        self.recovered = False
        self.call_counts = {name.value: 0 for name in IncidentToolName}
        self.mutations: list[dict[str, str]] = []
        self._definitions = self._build_definitions()

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    def registry(self) -> ToolRegistry:
        return ToolRegistry(self.definitions())

    def _build_definitions(self) -> tuple[ToolDefinition, ...]:
        read = ToolEffect.READ
        mutate = ToolEffect.MUTATE
        return (
            ToolDefinition(
                IncidentToolName.QUERY_SERVICE_HEALTH,
                SERVICE_SCHEMA,
                self._query_service_health,
                effect=read,
                description="Return current service and downstream health.",
            ),
            ToolDefinition(
                IncidentToolName.QUERY_METRICS,
                SERVICE_SCHEMA,
                self._query_metrics,
                effect=read,
                description="Return deterministic request and error metrics.",
            ),
            ToolDefinition(
                IncidentToolName.QUERY_LOGS,
                LOG_SCHEMA,
                self._query_logs,
                effect=read,
                description="Return recent structured service log records.",
            ),
            ToolDefinition(
                IncidentToolName.GET_RECENT_DEPLOYMENTS,
                SERVICE_SCHEMA,
                self._get_recent_deployments,
                effect=read,
                description="Return recent deployments in newest-first order.",
            ),
            ToolDefinition(
                IncidentToolName.READ_RUNBOOK,
                SERVICE_SCHEMA,
                self._read_runbook,
                effect=read,
                description="Return the matching service recovery runbook.",
            ),
            ToolDefinition(
                IncidentToolName.RESTART_SERVICE,
                SERVICE_SCHEMA,
                self._restart_service,
                effect=mutate,
                lane_argument="service_id",
                description="Restart an allowlisted service after approval.",
            ),
            ToolDefinition(
                IncidentToolName.ROLLBACK_SERVICE,
                ROLLBACK_SCHEMA,
                self._rollback_service,
                effect=mutate,
                lane_argument="service_id",
                description="Rollback an allowlisted service version after approval.",
            ),
            ToolDefinition(
                IncidentToolName.VERIFY_RECOVERY,
                VERIFY_SCHEMA,
                self._verify_recovery,
                effect=read,
                description="Verify health, version, and error-rate postconditions.",
            ),
        )

    def _result(self, name: IncidentToolName, payload: Mapping[str, Any]) -> str:
        self.call_counts[name.value] += 1
        return json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def _query_service_health(self, arguments: Mapping[str, Any]) -> str:
        service_id = str(arguments["service_id"])
        status = "healthy"
        version = "v1"
        payload: dict[str, Any] = {"service_id": service_id}
        if not self.recovered:
            if self.scenario is IncidentScenario.TRANSIENT_HANG and service_id in {
                "payment-service",
                "shop-api",
            }:
                status = "unhealthy"
                payload["cause"] = "payment_timeout"
            elif self.scenario is IncidentScenario.BAD_DEPLOYMENT and service_id in {
                "payment-service",
                "shop-api",
            }:
                status = "unhealthy"
                if service_id == "payment-service":
                    version = "v2"
                    payload["cause"] = "deployment_regression"
            elif self.scenario is IncidentScenario.DEPENDENCY_OUTAGE:
                if service_id in SERVICES:
                    status = "unhealthy"
                if service_id != "dependency-service":
                    payload["upstream"] = {
                        "service_id": "dependency-service",
                        "status": "unhealthy",
                    }
                else:
                    payload["cause"] = "service_unavailable"
        payload.update({"status": status, "version": version})
        return self._result(IncidentToolName.QUERY_SERVICE_HEALTH, payload)

    def _query_metrics(self, arguments: Mapping[str, Any]) -> str:
        service_id = str(arguments["service_id"])
        error_rate = 0.0
        if not self.recovered and service_id in {"payment-service", "shop-api"}:
            error_rate = {
                IncidentScenario.TRANSIENT_HANG: 0.35,
                IncidentScenario.BAD_DEPLOYMENT: 0.42,
                IncidentScenario.DEPENDENCY_OUTAGE: 0.37,
            }[self.scenario]
        return self._result(
            IncidentToolName.QUERY_METRICS,
            {"error_rate": error_rate, "requests": 100, "service_id": service_id},
        )


    def _query_logs(self, arguments: Mapping[str, Any]) -> str:
        service_id = str(arguments["service_id"])
        messages = {
            IncidentScenario.TRANSIENT_HANG: "payment request timed out",
            IncidentScenario.BAD_DEPLOYMENT: "PaymentRegressionError in version v2",
            IncidentScenario.DEPENDENCY_OUTAGE: "dependency-service unavailable",
        }
        records = [] if self.recovered else [
            {
                "level": "error",
                "message": messages[self.scenario],
                "service_id": service_id,
                "timestamp": "2026-01-15T10:31:04Z",
            }
        ]
        limit = int(arguments.get("limit", 20))
        return self._result(
            IncidentToolName.QUERY_LOGS,
            {"records": records[:limit], "service_id": service_id},
        )

    def _get_recent_deployments(self, arguments: Mapping[str, Any]) -> str:
        service_id = str(arguments["service_id"])
        deployments = [
            {
                "deployed_at": "2026-01-15T09:00:00Z",
                "service_id": service_id,
                "version": "v1",
            }
        ]
        if (
            self.scenario is IncidentScenario.BAD_DEPLOYMENT
            and not self.recovered
            and service_id == "payment-service"
        ):
            deployments.insert(
                0,
                {
                    "deployed_at": "2026-01-15T10:30:00Z",
                    "service_id": service_id,
                    "version": "v2",
                },
            )
        return self._result(
            IncidentToolName.GET_RECENT_DEPLOYMENTS,
            {"deployments": deployments, "service_id": service_id},
        )

    def _read_runbook(self, arguments: Mapping[str, Any]) -> str:
        action = {
            IncidentScenario.TRANSIENT_HANG: "restart_service",
            IncidentScenario.BAD_DEPLOYMENT: "rollback_service",
            IncidentScenario.DEPENDENCY_OUTAGE: "escalate_to_dependency_owner",
        }[self.scenario]
        return self._result(
            IncidentToolName.READ_RUNBOOK,
            {
                "recommended_action": action,
                "scenario": self.scenario.value,
                "service_id": str(arguments["service_id"]),
            },
        )

    def _restart_service(self, arguments: Mapping[str, Any]) -> str:
        if self.scenario is not IncidentScenario.TRANSIENT_HANG:
            raise ValueError("restart is not valid for this scenario")
        service_id = str(arguments["service_id"])
        self.recovered = True
        self.mutations.append({"action": "restart_service", "service_id": service_id})
        return self._result(
            IncidentToolName.RESTART_SERVICE,
            {"action": "restart_service", "service_id": service_id, "status": "completed"},
        )

    def _rollback_service(self, arguments: Mapping[str, Any]) -> str:
        if self.scenario is not IncidentScenario.BAD_DEPLOYMENT:
            raise ValueError("rollback is not valid for this scenario")
        service_id = str(arguments["service_id"])
        target_version = str(arguments["target_version"])
        self.recovered = True
        self.mutations.append(
            {
                "action": "rollback_service",
                "service_id": service_id,
                "target_version": target_version,
            }
        )
        return self._result(
            IncidentToolName.ROLLBACK_SERVICE,
            {
                "action": "rollback_service",
                "service_id": service_id,
                "status": "completed",
                "version": target_version,
            },
        )

    def _verify_recovery(self, arguments: Mapping[str, Any]) -> str:
        service_id = str(arguments["service_id"])
        expected_version = arguments.get("expected_version")
        current_version = "v1" if self.recovered or self.scenario is not IncidentScenario.BAD_DEPLOYMENT else "v2"
        verified = self.recovered and (
            expected_version is None or expected_version == current_version
        )
        return self._result(
            IncidentToolName.VERIFY_RECOVERY,
            {
                "error_rate": 0.0 if self.recovered else None,
                "service_id": service_id,
                "status": "healthy" if self.recovered else "unhealthy",
                "verified": verified,
                "version": current_version,
            },
        )


class DockerIncidentToolProvider:
    """Unified Incident tools backed by the allowlisted Docker Lab controller."""

    def __init__(self, lab_dir: str, scenario: IncidentScenario | str) -> None:
        self.controller = DockerLabController(lab_dir)
        self.scenario = IncidentScenario(scenario)
        templates = FakeIncidentToolProvider(self.scenario).definitions()
        handlers = {
            IncidentToolName.QUERY_SERVICE_HEALTH: self._health,
            IncidentToolName.QUERY_METRICS: self._metrics,
            IncidentToolName.QUERY_LOGS: self._logs,
            IncidentToolName.GET_RECENT_DEPLOYMENTS: self._deployments,
            IncidentToolName.READ_RUNBOOK: self._runbook,
            IncidentToolName.RESTART_SERVICE: self._restart,
            IncidentToolName.ROLLBACK_SERVICE: self._rollback,
            IncidentToolName.VERIFY_RECOVERY: self._verify,
        }
        self._definitions = tuple(
            replace(definition, handler=handlers[IncidentToolName(definition.name)])
            for definition in templates
        )

    def definitions(self) -> tuple[ToolDefinition, ...]:
        return self._definitions

    @staticmethod
    def _json(payload: Mapping[str, Any]) -> str:
        return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))

    def _health(self, arguments):
        return self._json(self.controller.query_health(arguments["service_id"]))

    def _metrics(self, arguments):
        return self._json(self.controller.query_metrics(arguments["service_id"]))

    def _logs(self, arguments):
        return self._json(
            self.controller.query_logs(
                arguments["service_id"], limit=arguments.get("limit", 20)
            )
        )

    def _deployments(self, arguments):
        health = self.controller.query_health(arguments["service_id"])
        return self._json(
            {
                "service_id": arguments["service_id"],
                "deployments": [{"version": health.get("version", "unknown")}],
            }
        )

    def _runbook(self, arguments):
        action = {
            IncidentScenario.TRANSIENT_HANG: "restart_service",
            IncidentScenario.BAD_DEPLOYMENT: "rollback_service",
            IncidentScenario.DEPENDENCY_OUTAGE: "escalate_to_dependency_owner",
        }[self.scenario]
        return self._json(
            {"service_id": arguments["service_id"], "recommended_action": action}
        )

    def _restart(self, arguments):
        self.controller.restart_service(arguments["service_id"])
        return self._json({"status": "completed", "action": "restart_service"})

    def _rollback(self, arguments):
        self.controller.rollback_service(
            arguments["service_id"], arguments["target_version"]
        )
        return self._json({"status": "completed", "action": "rollback_service"})

    def _verify(self, arguments):
        health = self.controller.wait_healthy(arguments["service_id"])
        expected = arguments.get("expected_version")
        verified = health.get("status") == "healthy" and (
            expected is None or health.get("version") == expected
        )
        return self._json({"verified": verified, "health": health})

from __future__ import annotations

import asyncio
import json

import pytest

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import (
    ApprovalDecision,
    RegistryToolExecutor,
    ToolEffect,
)
from incident_guard.tools import (
    FakeIncidentToolProvider,
    IncidentScenario,
    IncidentToolName,
)


class ApproveAll:
    def request_approval(self, request):
        return ApprovalDecision(request.request_id, True, "test approval")


def execute(executor, call_id, name, arguments):
    observation = asyncio.run(
        executor.execute(ToolCall(call_id, name, arguments))
    )
    return observation, json.loads(observation.content)


def test_provider_exposes_exact_unified_incident_tool_contract() -> None:
    provider = FakeIncidentToolProvider(IncidentScenario.BAD_DEPLOYMENT)
    definitions = provider.definitions()

    assert [definition.name for definition in definitions] == [
        name.value for name in IncidentToolName
    ]
    assert all(definition.description for definition in definitions)
    assert [definition.effect for definition in definitions[-3:]] == [
        ToolEffect.MUTATE,
        ToolEffect.MUTATE,
        ToolEffect.READ,
    ]
    for definition in definitions:
        assert definition.input_schema["additionalProperties"] is False
    for definition in definitions[-3:-1]:
        assert definition.requires_approval is True
        assert definition.lane_argument == "service_id"


def test_unified_schema_rejects_unknown_service_before_handler() -> None:
    provider = FakeIncidentToolProvider("transient_hang")
    executor = RegistryToolExecutor(provider.registry(), policy=provider.policy)

    observation, payload = execute(
        executor,
        "health-invalid",
        "query_service_health",
        {"service_id": "host-machine"},
    )

    assert observation.is_error is True
    assert payload["error"]["code"] == "invalid_arguments"
    assert provider.call_counts["query_service_health"] == 0


@pytest.mark.parametrize("scenario", list(IncidentScenario))
def test_all_read_tools_are_reproducible_without_docker(scenario) -> None:
    first_provider = FakeIncidentToolProvider(scenario)
    second_provider = FakeIncidentToolProvider(scenario)
    first_executor = RegistryToolExecutor(
        first_provider.registry(), policy=first_provider.policy
    )
    second_executor = RegistryToolExecutor(
        second_provider.registry(), policy=second_provider.policy
    )
    calls = (
        ("query_service_health", {"service_id": "payment-service"}),
        ("query_metrics", {"service_id": "payment-service"}),
        ("query_logs", {"service_id": "payment-service", "limit": 10}),
        ("get_recent_deployments", {"service_id": "payment-service"}),
        ("read_runbook", {"service_id": "payment-service"}),
        (
            "verify_recovery",
            {"service_id": "payment-service", "expected_version": "v1"},
        ),
    )

    first_results = [
        execute(first_executor, f"first-{index}", name, arguments)[0].content
        for index, (name, arguments) in enumerate(calls)
    ]
    second_results = [
        execute(second_executor, f"second-{index}", name, arguments)[0].content
        for index, (name, arguments) in enumerate(calls)
    ]

    assert first_results == second_results


@pytest.mark.parametrize(
    ("scenario", "action", "arguments", "unsafe_action", "unsafe_arguments"),
    [
        (
            IncidentScenario.TRANSIENT_HANG,
            "restart_service",
            {"service_id": "payment-service"},
            "rollback_service",
            {"service_id": "payment-service", "target_version": "v1"},
        ),
        (
            IncidentScenario.BAD_DEPLOYMENT,
            "rollback_service",
            {"service_id": "payment-service", "target_version": "v1"},
            "restart_service",
            {"service_id": "payment-service"},
        ),
    ],
)
def test_recoverable_scenarios_are_deterministic_and_policy_guarded(
    scenario, action, arguments, unsafe_action, unsafe_arguments
) -> None:
    provider = FakeIncidentToolProvider(scenario)
    executor = RegistryToolExecutor(
        provider.registry(), policy=provider.policy, approval_provider=ApproveAll()
    )

    first, first_payload = execute(
        executor,
        "health-1",
        "query_service_health",
        {"service_id": "payment-service"},
    )
    second, second_payload = execute(
        executor,
        "health-2",
        "query_service_health",
        {"service_id": "payment-service"},
    )
    denied, denied_payload = execute(
        executor, "unsafe-1", unsafe_action, unsafe_arguments
    )
    changed, changed_payload = execute(executor, "action-1", action, arguments)
    verified, verified_payload = execute(
        executor,
        "verify-1",
        "verify_recovery",
        {"service_id": "payment-service", "expected_version": "v1"},
    )

    assert first.content == second.content
    assert first_payload["status"] == second_payload["status"] == "unhealthy"
    assert denied.is_error is True
    assert denied_payload["error"]["code"] == "policy_denied"
    assert provider.call_counts[unsafe_action] == 0
    assert changed.is_error is False
    assert changed_payload["status"] == "completed"
    assert verified.is_error is False
    assert verified_payload == {
        "error_rate": 0.0,
        "service_id": "payment-service",
        "status": "healthy",
        "verified": True,
        "version": "v1",
    }
    assert provider.mutations[0]["action"] == action
    assert executor.approval_requests[0].lane == "service:payment-service"


def test_dependency_outage_recommends_escalation_and_denies_all_mutations() -> None:
    provider = FakeIncidentToolProvider("dependency_outage")
    executor = RegistryToolExecutor(
        provider.registry(), policy=provider.policy, approval_provider=ApproveAll()
    )

    health, health_payload = execute(
        executor,
        "health-1",
        "query_service_health",
        {"service_id": "payment-service"},
    )
    runbook, runbook_payload = execute(
        executor,
        "runbook-1",
        "read_runbook",
        {"service_id": "payment-service"},
    )
    restart, restart_payload = execute(
        executor,
        "restart-1",
        "restart_service",
        {"service_id": "payment-service"},
    )
    rollback, rollback_payload = execute(
        executor,
        "rollback-1",
        "rollback_service",
        {"service_id": "payment-service", "target_version": "v1"},
    )

    assert health.is_error is runbook.is_error is False
    assert health_payload["upstream"] == {
        "service_id": "dependency-service",
        "status": "unhealthy",
    }
    assert runbook_payload["recommended_action"] == "escalate_to_dependency_owner"
    assert restart_payload["error"]["code"] == "policy_denied"
    assert rollback_payload["error"]["code"] == "policy_denied"
    assert restart.is_error is rollback.is_error is True
    assert provider.call_counts["restart_service"] == 0
    assert provider.call_counts["rollback_service"] == 0
    assert provider.mutations == []
    assert executor.approval_requests == []

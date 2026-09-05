from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from incident_guard.agents.provider import (
    ProviderEvent,
    ProviderResponse,
    StopReason,
    ToolCall,
)
from incident_guard.agents.react_runtime import FakeToolExecutor, StructuredAgentRuntime
from incident_guard.agents.run_models import RunStatus
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)
from incident_guard.lab import DockerLabController


LAB_DIR = Path(__file__).parents[1] / "lab"


def events(response: ProviderResponse) -> list[ProviderEvent]:
    result = []
    if response.text:
        result.append(ProviderEvent.text_delta(response.text))
    result.extend(ProviderEvent.tool_call(call) for call in response.tool_calls)
    result.append(ProviderEvent.completed(response))
    return result


def wait_payment_downstream_unhealthy(
    controller: DockerLabController, timeout: float = 30
) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = controller.query_health("payment-service")
            if (
                last.get("status") == "unhealthy"
                and last.get("upstream", {}).get("status") == "unhealthy"
            ):
                return last
        except OSError:
            pass
        time.sleep(0.25)
    raise AssertionError(f"payment did not report dependency outage: {last}")


@pytest.mark.docker
def test_dependency_outage_escalates_without_restart_or_rollback() -> None:
    controller = DockerLabController(LAB_DIR)
    try:
        controller.reset()
        controller.inject_dependency_outage()
        health = wait_payment_downstream_unhealthy(controller)

        health_call = ToolCall(
            "health-1",
            "query_service_health",
            {"service_id": "payment-service"},
        )
        first = ProviderResponse(
            stop_reason=StopReason.TOOL_USE, tool_calls=(health_call,)
        )
        report = {
            "root_cause": "dependency_outage",
            "recommendation": "escalate_to_dependency_owner",
            "service": "dependency-service",
        }
        final = ProviderResponse(text=json.dumps(report, sort_keys=True))
        provider = ScriptedStreamingProvider([events(first), events(final)])
        executor = FakeToolExecutor(
            {"health-1": json.dumps(health, sort_keys=True)}
        )
        runtime = StructuredAgentRuntime(provider, executor)

        run = asyncio.run(
            runtime.run(
                "dependency-outage",
                [{"role": "user", "content": "investigate payment outage"}],
            )
        )

        assert health["version"] == "v1"
        assert run.status is RunStatus.COMPLETED
        assert json.loads(run.turns[0].final_response.text) == report
        assert [call.name for call in executor.calls] == ["query_service_health"]
        assert not any(
            call.name in {"restart_service", "rollback_service"}
            for call in executor.calls
        )
    finally:
        controller.down()

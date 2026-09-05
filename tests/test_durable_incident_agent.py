from __future__ import annotations

import json
from pathlib import Path

import pytest

from incident_guard.agents.provider import (
    ProviderEvent,
    ProviderResponse,
    StopReason,
    ToolCall,
)
from incident_guard.agents.provider_factory import ProviderConfig
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)
from incident_guard.durable_incident_agent import DurableIncidentAgentService
from incident_guard.lab import DockerLabController
from incident_guard.tools import (
    FakeIncidentToolProvider,
    IncidentScenario,
)


LAB_DIR = Path(__file__).parents[1] / "lab"


def _events(response: ProviderResponse):
    emitted = []
    if response.text:
        emitted.append(ProviderEvent.text_delta(response.text))
    emitted.extend(ProviderEvent.tool_call(call) for call in response.tool_calls)
    emitted.append(ProviderEvent.completed(response))
    return emitted


def _provider_config() -> ProviderConfig:
    return ProviderConfig(
        name="openai",
        api_key="test-key",
        base_url="https://api.deepseek.test",
        model="deepseek-test",
    )


class _FakeMCP:
    backend = FakeIncidentToolProvider(IncidentScenario.BAD_DEPLOYMENT)

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        pass

    def definitions(self):
        return self.backend.definitions()

    def registry(self):
        return self.backend.registry()


def test_durable_agent_pauses_and_resumes_across_service_processes(
    tmp_path, monkeypatch
) -> None:
    import incident_guard.durable_incident_agent as module

    _FakeMCP.backend = FakeIncidentToolProvider(IncidentScenario.BAD_DEPLOYMENT)
    providers = [
        ScriptedStreamingProvider(
            [
                _events(
                    ProviderResponse(
                        stop_reason=StopReason.TOOL_USE,
                        tool_calls=(
                            ToolCall(
                                "rollback-1",
                                "rollback_service",
                                {
                                    "service_id": "payment-service",
                                    "target_version": "v1",
                                },
                            ),
                        ),
                    )
                )
            ]
        ),
        ScriptedStreamingProvider(
            [_events(ProviderResponse(text='{"resolution":"recovered"}'))]
        ),
    ]
    monkeypatch.setattr(module, "MCPToolProvider", _FakeMCP)
    monkeypatch.setattr(
        module.ProviderConfig, "from_env", classmethod(lambda cls: _provider_config())
    )
    monkeypatch.setattr(
        module,
        "OpenAICompatibleProvider",
        lambda **_kwargs: providers.pop(0),
    )
    alert = tmp_path / "alert.json"
    alert.write_text(
        json.dumps({"service": "payment-service", "summary": "5xx spike"})
    )
    data_dir = tmp_path / "data"

    first = DurableIncidentAgentService(data_dir, LAB_DIR)
    waiting = first.investigate(
        alert,
        scenario=IncidentScenario.BAD_DEPLOYMENT,
        run_id="agent-durable",
    )
    first.close()
    assert waiting["status"] == "waiting_approval"
    assert waiting["pending_approvals"][0]["call_id"] == "rollback-1"
    assert _FakeMCP.backend.mutations == []

    operator = DurableIncidentAgentService(data_dir, LAB_DIR)
    approved = operator.decide(
        "agent-durable",
        "rollback-1",
        approved=True,
        reason="approved by operator",
    )
    operator.close()
    assert approved["status"] == "running"

    recovered = DurableIncidentAgentService(data_dir, LAB_DIR)
    completed = recovered.resume("agent-durable")
    events = recovered.store.replay("agent-durable")
    recovered.close()

    assert completed["status"] == "completed"
    assert _FakeMCP.backend.mutations == [
        {
            "action": "rollback_service",
            "service_id": "payment-service",
            "target_version": "v1",
        }
    ]
    event_types = [event.event_type for event in events]
    assert event_types.index("approval.decided") < event_types.index("tool.started")
    assert event_types.count("tool.completed") == 1


@pytest.mark.docker
def test_durable_agent_scripted_model_uses_real_mcp_docker_after_approval(
    tmp_path, monkeypatch
) -> None:
    import incident_guard.durable_incident_agent as module

    providers = [
        ScriptedStreamingProvider(
            [
                _events(
                    ProviderResponse(
                        stop_reason=StopReason.TOOL_USE,
                        tool_calls=(
                            ToolCall(
                                "rollback-1",
                                "rollback_service",
                                {
                                    "service_id": "payment-service",
                                    "target_version": "v1",
                                },
                            ),
                        ),
                    )
                )
            ]
        ),
        ScriptedStreamingProvider(
            [
                _events(
                    ProviderResponse(
                        stop_reason=StopReason.TOOL_USE,
                        tool_calls=(
                            ToolCall(
                                "verify-1",
                                "verify_recovery",
                                {
                                    "service_id": "payment-service",
                                    "expected_version": "v1",
                                },
                            ),
                        ),
                    )
                ),
                _events(ProviderResponse(text='{"resolution":"recovered"}')),
            ]
        ),
    ]
    monkeypatch.setattr(
        module.ProviderConfig, "from_env", classmethod(lambda cls: _provider_config())
    )
    monkeypatch.setattr(
        module,
        "OpenAICompatibleProvider",
        lambda **_kwargs: providers.pop(0),
    )
    alert = tmp_path / "alert.json"
    alert.write_text(
        json.dumps({"service": "payment-service", "summary": "5xx spike"})
    )
    data_dir = tmp_path / "data"
    controller = DockerLabController(LAB_DIR)

    try:
        controller.reset()
        controller.deploy_bad_deployment()
        first = DurableIncidentAgentService(data_dir, LAB_DIR)
        waiting = first.investigate(
            alert,
            scenario=IncidentScenario.BAD_DEPLOYMENT,
            run_id="agent-docker",
        )
        first.close()
        assert waiting["status"] == "waiting_approval"
        assert controller.query_health("payment-service")["version"] == "v2"

        operator = DurableIncidentAgentService(data_dir, LAB_DIR)
        operator.decide(
            "agent-docker",
            "rollback-1",
            approved=True,
            reason="approved by operator",
        )
        operator.close()

        resumed = DurableIncidentAgentService(data_dir, LAB_DIR)
        completed = resumed.resume("agent-docker")
        resumed.close()

        assert completed["status"] == "completed"
        assert [item["name"] for item in completed["tools"]] == [
            "rollback_service",
            "verify_recovery",
        ]
        assert controller.wait_healthy("payment-service")["version"] == "v1"
        assert controller.wait_healthy("shop-api")["status"] == "healthy"
    finally:
        controller.down()

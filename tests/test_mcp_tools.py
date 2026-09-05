from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters, types

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import (
    ApprovalDecision,
    RegistryToolExecutor,
)
from incident_guard.mcp import MCPToolProvider
from incident_guard.tools import (
    FakeIncidentToolProvider,
    IncidentScenarioPolicy,
    IncidentToolName,
)


ROOT = Path(__file__).parents[1]


class ApproveAll:
    def request_approval(self, request):
        return ApprovalDecision(request.request_id, True, "test approval")


def server_parameters(scenario: str = "bad_deployment") -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "incident_guard.mcp.server",
            "--scenario",
            scenario,
        ],
        cwd=ROOT,
    )


def test_stdio_discovery_call_and_outer_policy_approval() -> None:
    async def exercise() -> None:
        async with MCPToolProvider(server_parameters()) as provider:
            assert [definition.name for definition in provider.definitions()] == [
                name.value for name in IncidentToolName
            ]
            executor = RegistryToolExecutor(
                provider.registry(), policy=IncidentScenarioPolicy("bad_deployment")
            )
            denied = await executor.execute(
                ToolCall(
                    "rollback-denied",
                    "rollback_service",
                    {"service_id": "payment-service", "target_version": "v1"},
                )
            )
            assert json.loads(denied.content)["error"]["code"] == "approval_required"

            executor = RegistryToolExecutor(
                provider.registry(),
                policy=IncidentScenarioPolicy("bad_deployment"),
                approval_provider=ApproveAll(),
            )
            changed = await executor.execute(
                ToolCall(
                    "rollback-approved",
                    "rollback_service",
                    {"service_id": "payment-service", "target_version": "v1"},
                )
            )
            verified = await executor.execute(
                ToolCall(
                    "verify",
                    "verify_recovery",
                    {"service_id": "payment-service", "expected_version": "v1"},
                )
            )

            assert changed.is_error is False
            assert json.loads(changed.content)["status"] == "completed"
            assert json.loads(verified.content)["verified"] is True
            assert executor.approval_requests[0].lane == "service:payment-service"

    asyncio.run(exercise())


class StubSession:
    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def list_tools(self):
        definitions = FakeIncidentToolProvider("transient_hang").definitions()
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=str(definition.name),
                    description=definition.description,
                    inputSchema=dict(definition.input_schema),
                )
                for definition in definitions
            ]
        )

    async def call_tool(self, name, arguments):
        if self.mode == "timeout":
            await asyncio.sleep(0.05)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="remote failure")],
            isError=True,
        )


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [("error", "execution_failed"), ("timeout", "timeout")],
)
def test_remote_error_and_timeout_become_stable_observations(
    mode, expected_code
) -> None:
    async def exercise():
        provider = MCPToolProvider(server_parameters(), call_timeout=0.01)
        provider._session = StubSession(mode)
        await provider.discover()
        executor = RegistryToolExecutor(provider.registry())
        return await executor.execute(
            ToolCall(
                "health-1",
                "query_service_health",
                {"service_id": "payment-service"},
            )
        )

    observation = asyncio.run(exercise())
    payload = json.loads(observation.content)

    assert observation.is_error is True
    assert payload["error"]["code"] == expected_code

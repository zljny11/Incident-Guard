from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp import StdioServerParameters

from incident_guard.agents.event_runtime import (
    DurableApprovalProvider,
    EventDrivenAgentRuntime,
    decide_durable_approval,
)
from incident_guard.agents.openai_compatible_provider import OpenAICompatibleProvider
from incident_guard.agents.provider_factory import ProviderConfig
from incident_guard.agents.react_runtime import RunLimits
from incident_guard.agents.tool_pipeline import RegistryToolExecutor, ToolDefinition
from incident_guard.context import ApprovalStatus, IncidentStateProjector
from incident_guard.events import RunEventProjector, SQLiteEventStore
from incident_guard.mcp import MCPToolProvider
from incident_guard.tools import IncidentScenario, IncidentScenarioPolicy


DURABLE_AGENT_PROMPT = """You are an incident-response agent for an isolated Docker lab.
Use the supplied tools to investigate the alert before proposing a recovery action.
Corroborate the diagnosis with health, metrics, logs, deployments, or runbook evidence.
Mutating tools are controlled by policy and pause for explicit operator approval.
After an approved mutation, call verify_recovery and check the actual postcondition.
If the fault belongs to a downstream dependency, avoid changing a healthy application
deployment and return a justified escalation instead. Do not repeat successful calls.

When work is complete, return only one JSON object:
{"root_cause":"concise_diagnosis_or_unknown",
 "resolution":"executed_action_or_safe_escalation_or_unresolved",
 "summary":"short evidence-based explanation"}
"""


class DurableIncidentAgentError(RuntimeError):
    pass


def _tool_specs(
    definitions: tuple[ToolDefinition, ...],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "type": "function",
            "function": {
                "name": definition.name,
                "description": definition.description,
                "parameters": dict(definition.input_schema),
            },
        }
        for definition in definitions
    )


class DurableIncidentAgentService:
    """DeepSeek -> durable runtime -> approval -> MCP -> Docker application service."""

    def __init__(self, data_dir: str | Path, lab_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lab_dir = Path(lab_dir).resolve()
        self.store = SQLiteEventStore(self.data_dir / "events.db")
        self.projector = RunEventProjector()
        self.incident_projector = IncidentStateProjector(self.projector)
        self.metadata_dir = self.data_dir / "agent-runs"
        self.metadata_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        self.store.close()

    def investigate(
        self,
        alert_path: str | Path,
        *,
        scenario: IncidentScenario | str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        alert = json.loads(Path(alert_path).read_text())
        if not isinstance(alert, dict):
            raise DurableIncidentAgentError("alert must be a JSON object")
        selected = IncidentScenario(scenario or self._current_scenario())
        resolved_run_id = run_id or f"agent-{uuid4().hex[:12]}"
        if self.store.replay(resolved_run_id):
            raise DurableIncidentAgentError(f"run already exists: {resolved_run_id}")
        self._write_metadata(
            resolved_run_id,
            {"run_id": resolved_run_id, "scenario": selected.value},
        )
        projection = asyncio.run(
            self._execute(
                resolved_run_id,
                selected,
                messages=[
                    {"role": "system", "content": DURABLE_AGENT_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            alert,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    },
                ],
            )
        )
        return self._summary(resolved_run_id, projection)

    def resume(self, run_id: str) -> dict[str, Any]:
        metadata = self._read_metadata(run_id)
        projection = asyncio.run(
            self._execute(
                run_id,
                IncidentScenario(metadata["scenario"]),
                messages=None,
            )
        )
        return self._summary(run_id, projection)

    def decide(
        self,
        run_id: str,
        call_id: str,
        *,
        approved: bool,
        reason: str,
    ) -> dict[str, Any]:
        self._read_metadata(run_id)
        projection = decide_durable_approval(
            self.store,
            run_id,
            call_id,
            approved=approved,
            reason=reason,
            projector=self.projector,
        )
        return self._summary(run_id, projection)

    def status(self, run_id: str) -> dict[str, Any]:
        self._read_metadata(run_id)
        events = self.store.replay(run_id)
        if not events:
            raise DurableIncidentAgentError(f"run does not exist: {run_id}")
        return self._summary(run_id, self.projector.project(run_id, events))

    async def _execute(
        self,
        run_id: str,
        scenario: IncidentScenario,
        *,
        messages: list[dict[str, Any]] | None,
    ):
        config = ProviderConfig.from_env()
        if config.name != "openai" or not config.api_key or not config.model:
            raise DurableIncidentAgentError(
                "durable agent requires IG_PROVIDER=openai, "
                "IG_OPENAI_API_KEY, and IG_OPENAI_MODEL"
            )
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "incident_guard.mcp.server",
                "--backend",
                "docker",
                "--scenario",
                scenario.value,
                "--lab-dir",
                str(self.lab_dir),
            ],
            cwd=str(Path.cwd()),
        )
        async with MCPToolProvider(parameters, call_timeout=180) as tools:
            definitions = tools.definitions()
            extra_body: Mapping[str, Any] = {}
            if "deepseek" in config.base_url.lower() or "deepseek" in config.model.lower():
                extra_body = {"thinking": {"type": "disabled"}}
            provider = OpenAICompatibleProvider(
                api_key=config.api_key,
                base_url=config.base_url,
                model=config.model,
                timeout_seconds=config.timeout_seconds,
                tools=_tool_specs(definitions),
                temperature=0,
                response_format={"type": "json_object"},
                extra_body=extra_body,
            )
            executor = RegistryToolExecutor(
                tools.registry(),
                policy=IncidentScenarioPolicy(scenario),
                approval_provider=DurableApprovalProvider(self.store, run_id),
            )
            runtime = EventDrivenAgentRuntime(
                provider,
                executor,
                self.store,
                limits=RunLimits(
                    max_steps=12,
                    timeout_seconds=300,
                    max_tool_calls=40,
                    max_tokens=120_000,
                ),
            )
            if messages is not None:
                return await runtime.run(run_id, messages)
            return await runtime.resume(run_id)

    def _summary(self, run_id: str, projection) -> dict[str, Any]:
        state = self.incident_projector.project(
            run_id, self.store.replay(run_id)
        )
        pending = [
            {
                "request_id": item.request_id,
                "call_id": item.call_id,
                "status": item.status.value,
                "reason": item.reason,
            }
            for item in state.approvals
            if item.status is ApprovalStatus.PENDING
        ]
        input_tokens = sum(
            item.input_tokens or 0 for item in projection.assistant_records
        )
        output_tokens = sum(
            item.output_tokens or 0 for item in projection.assistant_records
        )
        return {
            "run_id": run_id,
            "mode": "deepseek-durable-mcp-docker",
            "status": projection.status.value,
            "last_sequence": projection.last_sequence,
            "failure_reason": projection.failure_reason,
            "pending_approvals": pending,
            "tools": [
                {
                    "call_id": tool.call_id,
                    "name": tool.name,
                    "effect": tool.effect,
                    "state": tool.state.value,
                }
                for tool in projection.tools.values()
            ],
            "final_response": (
                projection.assistant_records[-1].text
                if projection.assistant_records
                and projection.status.is_terminal
                else None
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def _metadata_path(self, run_id: str) -> Path:
        digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
        return self.metadata_dir / f"{digest}.json"

    def _write_metadata(self, run_id: str, payload: Mapping[str, Any]) -> None:
        self._metadata_path(run_id).write_text(
            json.dumps(dict(payload), ensure_ascii=False, sort_keys=True) + "\n"
        )

    def _read_metadata(self, run_id: str) -> dict[str, Any]:
        path = self._metadata_path(run_id)
        if not path.is_file():
            raise DurableIncidentAgentError(f"durable agent run does not exist: {run_id}")
        payload = json.loads(path.read_text())
        if payload.get("run_id") != run_id or payload.get("scenario") not in {
            item.value for item in IncidentScenario
        }:
            raise DurableIncidentAgentError("invalid durable agent run metadata")
        return payload

    @property
    def _state_path(self) -> Path:
        return self.data_dir / "lab-state.json"

    def _current_scenario(self) -> str:
        if not self._state_path.is_file():
            raise DurableIncidentAgentError("inject a lab scenario before investigate")
        return str(json.loads(self._state_path.read_text())["scenario"])

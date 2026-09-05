from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp import StdioServerParameters

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import ApprovalDecision, RegistryToolExecutor
from incident_guard.context import ApprovalStatus, IncidentStateProjector
from incident_guard.events import NewRunEvent, RunEventProjector, SQLiteEventStore
from incident_guard.lab import DockerLabController
from incident_guard.mcp import MCPToolProvider
from incident_guard.tools import IncidentScenario, IncidentScenarioPolicy


class IncidentCLIError(RuntimeError):
    pass


class IncidentCLIService:
    """Durable orchestration used by the recording-oriented CLI commands."""

    def __init__(self, data_dir: str | Path, lab_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lab_dir = Path(lab_dir)
        self.store = SQLiteEventStore(self.data_dir / "events.db")
        self.projector = RunEventProjector()
        self.incident_projector = IncidentStateProjector(self.projector)

    def close(self) -> None:
        self.store.close()

    def lab(self, action: str) -> dict[str, Any]:
        controller = DockerLabController(self.lab_dir)
        if action == "up":
            controller.up()
        elif action == "down":
            controller.down()
        elif action == "reset":
            controller.reset()
        else:
            raise IncidentCLIError(f"unknown lab action: {action}")
        return {"action": action, "status": "completed"}

    def inject(self, scenario: IncidentScenario | str) -> dict[str, Any]:
        scenario = IncidentScenario(scenario)
        controller = DockerLabController(self.lab_dir)
        if scenario is IncidentScenario.TRANSIENT_HANG:
            result = controller.inject_transient_hang()
        elif scenario is IncidentScenario.BAD_DEPLOYMENT:
            controller.deploy_bad_deployment()
            result = {"service": "payment-service", "version": "v2"}
        else:
            controller.inject_dependency_outage()
            result = {"service": "dependency-service", "status": "stopped"}
        self._state_path.write_text(
            json.dumps({"scenario": scenario.value}, sort_keys=True) + "\n"
        )
        return {"scenario": scenario.value, "status": "injected", "result": result}

    def investigate(
        self,
        alert_path: str | Path,
        *,
        scenario: IncidentScenario | str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        alert = json.loads(Path(alert_path).read_text())
        if not isinstance(alert, dict):
            raise IncidentCLIError("alert must be a JSON object")
        selected = IncidentScenario(scenario or self._current_scenario())
        run_id = run_id or f"run-{uuid4().hex[:12]}"
        if self.store.replay(run_id):
            raise IncidentCLIError(f"run already exists: {run_id}")

        reads = self._collect_read_results(selected)
        calls = [
            {
                "id": f"{name}-{index}",
                "name": name,
                "arguments": {"service_id": "payment-service"},
            }
            for index, name in enumerate(reads, 1)
        ]
        events = [
            NewRunEvent("run.started"),
            NewRunEvent(
                "alert.received",
                {"role": "user", "content": json.dumps(alert, sort_keys=True)},
            ),
            NewRunEvent(
                "goal.set",
                {"role": "system", "content": "restore payment-service safely"},
            ),
            NewRunEvent("turn.started", {"turn_number": 1}),
            NewRunEvent("step.started", {"turn_number": 1, "step_number": 1}),
            NewRunEvent(
                "assistant.message",
                {
                    "turn_number": 1,
                    "step_number": 1,
                    "text": "Collecting incident evidence",
                    "stop_reason": "tool_use",
                    "tool_calls": calls,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            ),
        ]
        for index, (name, result) in enumerate(reads.items(), 1):
            call_id = f"{name}-{index}"
            events.extend(
                (
                    NewRunEvent(
                        "tool.requested",
                        {
                            "call_id": call_id,
                            "name": name,
                            "arguments": {"service_id": "payment-service"},
                            "effect": "read",
                            "call_index": index - 1,
                        },
                    ),
                    NewRunEvent(
                        "tool.started",
                        {"call_id": call_id, "name": name, "effect": "read"},
                    ),
                    NewRunEvent(
                        "tool.completed",
                        {
                            "call_id": call_id,
                            "name": name,
                            "content": json.dumps(result, sort_keys=True),
                            "is_error": False,
                        },
                    ),
                    NewRunEvent(
                        "evidence.recorded",
                        {
                            "role": "system",
                            "content": json.dumps(result, sort_keys=True),
                            "evidence_id": f"evidence-{index}",
                        },
                    ),
                )
            )
        events.extend(
            (
                NewRunEvent(
                    "hypothesis.updated",
                    {
                        "statement": selected.value,
                        "evidence_ids": [f"evidence-{index}" for index in range(1, 6)],
                    },
                ),
                NewRunEvent(
                    "step.completed", {"turn_number": 1, "step_number": 1}
                ),
            )
        )

        if selected is IncidentScenario.DEPENDENCY_OUTAGE:
            events.extend(self._completion_events(2, selected, escalation=True))
        else:
            action = (
                "restart_service"
                if selected is IncidentScenario.TRANSIENT_HANG
                else "rollback_service"
            )
            arguments = {"service_id": "payment-service"}
            if action == "rollback_service":
                arguments["target_version"] = "v1"
            call_id = f"{action}-1"
            events.extend(
                (
                    NewRunEvent(
                        "step.started", {"turn_number": 1, "step_number": 2}
                    ),
                    NewRunEvent(
                        "assistant.message",
                        {
                            "turn_number": 1,
                            "step_number": 2,
                            "text": f"Proposed recovery: {action}",
                            "stop_reason": "tool_use",
                            "tool_calls": [
                                {"id": call_id, "name": action, "arguments": arguments}
                            ],
                            "input_tokens": 0,
                            "output_tokens": 0,
                        },
                    ),
                    NewRunEvent(
                        "tool.requested",
                        {
                            "call_id": call_id,
                            "name": action,
                            "arguments": arguments,
                            "effect": "mutate",
                            "call_index": 0,
                        },
                    ),
                    NewRunEvent(
                        "approval.requested",
                        {
                            "request_id": f"approval:{call_id}",
                            "call_id": call_id,
                            "reason": f"operator approval required for {action}",
                        },
                    ),
                )
            )
        self.store.append_batch(run_id, events)
        projection = self.projector.project(run_id, self.store.replay(run_id))
        return self._summary(projection)

    def decide(
        self, run_id: str, call_id: str, *, approved: bool, reason: str
    ) -> dict[str, Any]:
        state = self._state(run_id)
        pending = next(
            (
                item
                for item in state.approvals
                if item.call_id == call_id and item.status is ApprovalStatus.PENDING
            ),
            None,
        )
        if pending is None:
            raise IncidentCLIError(f"no pending approval for call: {call_id}")
        self.store.append(
            run_id,
            NewRunEvent(
                "approval.decided",
                {
                    "request_id": pending.request_id,
                    "approved": approved,
                    "reason": reason,
                },
            ),
        )
        if not approved:
            self.store.append(
                run_id,
                NewRunEvent("run.failed", {"reason": f"operator rejected {call_id}"}),
            )
        return self.status(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        projection = self._projection(run_id)
        if projection.status.is_terminal:
            return self._summary(projection)
        state = self._state(run_id)
        approved = next(
            (item for item in state.approvals if item.status is ApprovalStatus.APPROVED),
            None,
        )
        if approved is None:
            raise IncidentCLIError("run has no approved pending action")
        tool = projection.tools[approved.call_id]
        self.store.append(
            run_id,
            NewRunEvent(
                "tool.started",
                {"call_id": tool.call_id, "name": tool.name, "effect": "mutate"},
            ),
        )
        try:
            result = self._perform_recovery(tool, self._scenario_for_tool(tool.name))
            self.store.append(
                run_id,
                NewRunEvent(
                    "tool.completed",
                    {
                        "call_id": tool.call_id,
                        "name": tool.name,
                        "content": json.dumps(result, sort_keys=True),
                        "is_error": False,
                    },
                ),
            )
        except Exception as error:
            self.store.append(
                run_id,
                NewRunEvent(
                    "run.failed_uncertain",
                    {"reason": f"mutation outcome uncertain: {type(error).__name__}"},
                ),
            )
            return self.status(run_id)

        self.store.append(
            run_id,
            NewRunEvent("step.completed", {"turn_number": 1, "step_number": 2}),
        )
        scenario = self._scenario_for_tool(tool.name)
        verification = self._verify_recovery(scenario)
        if not verification.get("verified"):
            raise IncidentCLIError("recovery verification failed")
        recovered = verification["health"]
        events = [
            NewRunEvent(
                "evidence.recorded",
                {
                    "role": "system",
                    "content": json.dumps(recovered, sort_keys=True),
                    "evidence_id": "recovery-health",
                },
            ),
            NewRunEvent(
                "fact.confirmed",
                {
                    "fact_id": "recovery-verified",
                    "statement": "payment-service recovered on v1",
                    "evidence_ids": ["recovery-health"],
                },
            ),
            *self._verification_events(3, recovered),
            *self._completion_events(
                4,
                IncidentScenario.TRANSIENT_HANG
                if tool.name == "restart_service"
                else IncidentScenario.BAD_DEPLOYMENT,
            ),
        ]
        self.store.append_batch(run_id, events)
        return self.status(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        projection = self._projection(run_id)
        if not projection.status.is_terminal:
            self.store.append_batch(
                run_id,
                (NewRunEvent("run.cancelling"), NewRunEvent("run.cancelled")),
            )
        return self.status(run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        projection = self._projection(run_id)
        summary = self._summary(projection)
        state = self._state(run_id)
        summary.update(
            {
                "approvals": [
                    {
                        "call_id": item.call_id,
                        "request_id": item.request_id,
                        "status": item.status.value,
                    }
                    for item in state.approvals
                ],
                "evidence": [
                    {
                        "evidence_id": item.evidence_id,
                        "source_sequence": item.source_sequence,
                        "summary": item.summary,
                    }
                    for item in state.evidence
                ],
                "evidence_count": len(state.evidence),
                "hypothesis": (
                    state.current_hypothesis.statement
                    if state.current_hypothesis is not None
                    else None
                ),
                "final_response": (
                    projection.assistant_records[-1].text
                    if projection.assistant_records
                    else None
                ),
                "tools": [
                    {"call_id": item.call_id, "name": item.name, "state": item.state.value}
                    for item in projection.tools.values()
                ],
            }
        )
        return summary

    def list_runs(self) -> list[dict[str, Any]]:
        return [self.status(run_id) for run_id in self.store.list_run_ids()]

    def timeline(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        events = self.store.replay(run_id, after_sequence=after_sequence)
        if not events and after_sequence == 0:
            raise IncidentCLIError(f"run does not exist: {run_id}")
        return [
            {
                "event_id": event.event_id,
                "run_id": event.run_id,
                "sequence": event.sequence,
                "event_type": event.event_type,
                "payload": dict(event.payload),
                "schema_version": event.schema_version,
                "occurred_at": event.occurred_at,
            }
            for event in events
        ]

    def _collect_read_results(self, scenario: IncidentScenario) -> dict[str, Any]:
        names = (
            "query_service_health",
            "query_metrics",
            "query_logs",
            "get_recent_deployments",
            "read_runbook",
        )
        calls = [
            ToolCall(f"cli-{index}", name, {"service_id": "payment-service"})
            for index, name in enumerate(names, 1)
        ]
        values = self._mcp_calls(scenario, calls)
        return dict(zip(names, values, strict=True))

    def _perform_recovery(self, tool, scenario: IncidentScenario) -> dict[str, Any]:
        result = self._mcp_calls(
            scenario,
            [ToolCall(tool.call_id, tool.name, dict(tool.arguments))],
            approve=True,
        )[0]
        return result

    def _verify_recovery(self, scenario: IncidentScenario) -> dict[str, Any]:
        return self._mcp_calls(
            scenario,
            [
                ToolCall(
                    "verify-mcp",
                    "verify_recovery",
                    {"service_id": "payment-service", "expected_version": "v1"},
                )
            ],
        )[0]

    def _mcp_calls(self, scenario, calls, *, approve=False):
        class DurableApproval:
            def request_approval(self, request):
                return ApprovalDecision(request.request_id, True, "durably approved")

        async def execute():
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
                    str(self.lab_dir.resolve()),
                ],
                cwd=str(Path.cwd()),
            )
            async with MCPToolProvider(parameters, call_timeout=180) as provider:
                executor = RegistryToolExecutor(
                    provider.registry(),
                    policy=IncidentScenarioPolicy(scenario),
                    approval_provider=DurableApproval() if approve else None,
                )
                observations = [await executor.execute(call) for call in calls]
                if any(item.is_error for item in observations):
                    raise IncidentCLIError(
                        next(item.content for item in observations if item.is_error)
                    )
                return [json.loads(item.content) for item in observations]

        return asyncio.run(execute())

    @staticmethod
    def _scenario_for_tool(name: str) -> IncidentScenario:
        return (
            IncidentScenario.TRANSIENT_HANG
            if name == "restart_service"
            else IncidentScenario.BAD_DEPLOYMENT
        )

    @staticmethod
    def _verification_events(step, recovered):
        call = {
            "id": "verify_recovery-1",
            "name": "verify_recovery",
            "arguments": {"service_id": "payment-service", "expected_version": "v1"},
        }
        return (
            NewRunEvent("step.started", {"turn_number": 1, "step_number": step}),
            NewRunEvent(
                "assistant.message",
                {
                    "turn_number": 1,
                    "step_number": step,
                    "text": "Verifying recovery",
                    "stop_reason": "tool_use",
                    "tool_calls": [call],
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            ),
            NewRunEvent(
                "tool.requested",
                {**call, "call_id": call["id"], "effect": "read", "call_index": 0},
            ),
            NewRunEvent(
                "tool.started",
                {"call_id": call["id"], "name": call["name"], "effect": "read"},
            ),
            NewRunEvent(
                "tool.completed",
                {
                    "call_id": call["id"],
                    "name": call["name"],
                    "content": json.dumps({"verified": True, "health": recovered}, sort_keys=True),
                    "is_error": False,
                },
            ),
            NewRunEvent("step.completed", {"turn_number": 1, "step_number": step}),
        )

    @staticmethod
    def _completion_events(step, scenario, escalation=False):
        report = {
            "root_cause": scenario.value,
            "resolution": "escalate_to_dependency_owner" if escalation else "recovered",
        }
        return (
            NewRunEvent("step.started", {"turn_number": 1, "step_number": step}),
            NewRunEvent(
                "assistant.message",
                {
                    "turn_number": 1,
                    "step_number": step,
                    "text": json.dumps(report, sort_keys=True),
                    "stop_reason": "end_turn",
                    "tool_calls": [],
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            ),
            NewRunEvent("step.completed", {"turn_number": 1, "step_number": step}),
            NewRunEvent("run.completed"),
        )

    @property
    def _state_path(self) -> Path:
        return self.data_dir / "lab-state.json"

    def _current_scenario(self) -> str:
        if not self._state_path.is_file():
            raise IncidentCLIError("inject a lab scenario before investigate")
        return str(json.loads(self._state_path.read_text())["scenario"])

    def _projection(self, run_id):
        events = self.store.replay(run_id)
        if not events:
            raise IncidentCLIError(f"run does not exist: {run_id}")
        return self.projector.project(run_id, events)

    def _state(self, run_id):
        events = self.store.replay(run_id)
        if not events:
            raise IncidentCLIError(f"run does not exist: {run_id}")
        return self.incident_projector.project(run_id, events)

    @staticmethod
    def _summary(projection):
        return {
            "failure_reason": projection.failure_reason,
            "last_sequence": projection.last_sequence,
            "run_id": projection.run_id,
            "status": projection.status.value,
        }

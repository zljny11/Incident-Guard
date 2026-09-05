from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import (
    ApprovalDecision,
    RegistryToolExecutor,
)
from incident_guard.tools import FakeIncidentToolProvider, IncidentScenarioPolicy


class BaselineState(TypedDict, total=False):
    alert: dict[str, Any]
    evidence: dict[str, Any]
    root_cause: str
    proposed_action: str
    approved: bool
    mutation_result: dict[str, Any]
    verification: dict[str, Any]
    report: dict[str, Any]


class AutoApproveToolBoundary:
    def request_approval(self, request) -> ApprovalDecision:
        return ApprovalDecision(
            request.request_id,
            True,
            "graph interrupt already captured operator approval",
        )


def _execute(executor: RegistryToolExecutor, calls: tuple[ToolCall, ...]):
    observations = asyncio.run(executor.execute_batch(calls))
    result = {}
    for observation in observations:
        if observation.is_error:
            raise RuntimeError(observation.content)
        result[observation.name] = json.loads(observation.content)
    return result


def build_bad_deployment_graph(
    tool_provider: FakeIncidentToolProvider | None = None,
):
    provider = tool_provider or FakeIncidentToolProvider("bad_deployment")
    executor = RegistryToolExecutor(
        provider.registry(),
        policy=IncidentScenarioPolicy("bad_deployment"),
        approval_provider=AutoApproveToolBoundary(),
    )

    def collect_evidence(_state: BaselineState) -> BaselineState:
        names = (
            "query_service_health",
            "query_metrics",
            "query_logs",
            "get_recent_deployments",
            "read_runbook",
        )
        calls = tuple(
            ToolCall(
                f"baseline-{name}",
                name,
                {
                    "service_id": "payment-service",
                    **({"limit": 20} if name == "query_logs" else {}),
                },
            )
            for name in names
        )
        return {"evidence": _execute(executor, calls)}

    def diagnose(state: BaselineState) -> BaselineState:
        evidence = state["evidence"]
        deployments = evidence["get_recent_deployments"]["deployments"]
        logs = json.dumps(evidence["query_logs"]).lower()
        is_regression = (
            evidence["query_metrics"]["error_rate"] >= 0.3
            and deployments[0]["version"] == "v2"
            and "regression" in logs
        )
        return {
            "root_cause": "bad_deployment" if is_regression else "unknown",
            "proposed_action": "rollback_service" if is_regression else "escalate",
        }

    def request_approval(state: BaselineState) -> BaselineState:
        approved = interrupt(
            {
                "action": state["proposed_action"],
                "service_id": "payment-service",
                "target_version": "v1",
            }
        )
        return {"approved": bool(approved)}

    def after_approval(state: BaselineState) -> Literal["approved", "rejected"]:
        return "approved" if state.get("approved") else "rejected"

    def remediate(_state: BaselineState) -> BaselineState:
        result = _execute(
            executor,
            (
                ToolCall(
                    "baseline-rollback",
                    "rollback_service",
                    {"service_id": "payment-service", "target_version": "v1"},
                ),
            ),
        )
        return {"mutation_result": result["rollback_service"]}

    def verify(_state: BaselineState) -> BaselineState:
        result = _execute(
            executor,
            (
                ToolCall(
                    "baseline-verify",
                    "verify_recovery",
                    {"service_id": "payment-service", "expected_version": "v1"},
                ),
            ),
        )
        return {"verification": result["verify_recovery"]}

    def report(state: BaselineState) -> BaselineState:
        verified = state.get("verification", {}).get("verified") is True
        return {
            "report": {
                "root_cause": state.get("root_cause"),
                "resolution": (
                    "rollback_service" if verified else "operator_rejected"
                ),
                "recovery_verified": verified,
            }
        }

    builder = StateGraph(BaselineState)
    builder.add_node("collect_evidence", collect_evidence)
    builder.add_node("diagnose", diagnose)
    builder.add_node("request_approval", request_approval)
    builder.add_node("remediate", remediate)
    builder.add_node("verify", verify)
    builder.add_node("report", report)
    builder.add_edge(START, "collect_evidence")
    builder.add_edge("collect_evidence", "diagnose")
    builder.add_edge("diagnose", "request_approval")
    builder.add_conditional_edges(
        "request_approval",
        after_approval,
        {"approved": "remediate", "rejected": "report"},
    )
    builder.add_edge("remediate", "verify")
    builder.add_edge("verify", "report")
    builder.add_edge("report", END)
    return builder.compile(checkpointer=InMemorySaver()), provider, executor


def run_bad_deployment_baseline(
    *, approved: bool = True, thread_id: str = "langgraph-bad-deployment"
) -> dict[str, Any]:
    graph, provider, executor = build_bad_deployment_graph()
    config = {"configurable": {"thread_id": thread_id}}
    pending = graph.invoke(
        {
            "alert": {
                "service": "payment-service",
                "summary": "payment error rate exceeds 30%",
            }
        },
        config,
    )
    mutations_before_approval = len(provider.mutations)
    interrupt_count = len(pending.get("__interrupt__", ()))
    final = graph.invoke(Command(resume=approved), config)
    checkpoints = list(graph.get_state_history(config))
    return {
        "baseline": "langgraph",
        "scenario_id": "bad_deployment",
        "approved": approved,
        "interrupt_count": interrupt_count,
        "checkpoint_count": len(checkpoints),
        "mutations_before_approval": mutations_before_approval,
        "mutation_count": len(provider.mutations),
        "tool_approval_count": len(executor.approval_decisions),
        "root_cause": final.get("root_cause"),
        "recovery_verified": final.get("verification", {}).get("verified", False),
        "report": final.get("report", {}),
        "graph": {
            "nodes": 6,
            "conditional_routes": 1,
            "explicit_state_fields": len(BaselineState.__annotations__),
        },
    }


def write_langgraph_baseline_report(output_dir: str | Path) -> dict[str, Any]:
    report = {
        "approved_run": run_bad_deployment_baseline(approved=True),
        "rejected_run": run_bad_deployment_baseline(
            approved=False, thread_id="langgraph-bad-deployment-rejected"
        ),
    }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "langgraph-baseline.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    approved = report["approved_run"]
    rejected = report["rejected_run"]
    markdown = f"""# LangGraph Baseline

| Case | Interrupts | Mutations before approval | Mutations | Verified |
| --- | ---: | ---: | ---: | --- |
| approved | {approved['interrupt_count']} | {approved['mutations_before_approval']} | {approved['mutation_count']} | {approved['recovery_verified']} |
| rejected | {rejected['interrupt_count']} | {rejected['mutations_before_approval']} | {rejected['mutation_count']} | {rejected['recovery_verified']} |

Graph nodes: {approved['graph']['nodes']}  
Explicit state fields: {approved['graph']['explicit_state_fields']}
"""
    (output / "langgraph-baseline.md").write_text(markdown)
    return report

from __future__ import annotations

import asyncio

import pytest

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import (
    ApprovalDecision,
    RegistryToolExecutor,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)


SCHEMA = {
    "type": "object",
    "properties": {"service_id": {"type": "string"}},
    "required": ["service_id"],
}


class DecidingApprover:
    def __init__(self, approved):
        self.approved = approved
        self.requests = []

    def request_approval(self, request):
        self.requests.append(request)
        return ApprovalDecision(request.request_id, self.approved, "operator decision")


class ApproveAll:
    def request_approval(self, request):
        return ApprovalDecision(request.request_id, True)


def test_mutation_definition_cannot_disable_approval() -> None:
    with pytest.raises(ValueError, match="MUTATE tools must require approval"):
        ToolDefinition(
            "restart_service",
            SCHEMA,
            lambda _arguments: "restarted",
            effect="mutate",
            requires_approval=False,
        )


@pytest.mark.parametrize(
    ("approved", "expected_calls", "expected_code"),
    [(False, 0, "approval_denied"), (True, 1, None)],
)
def test_mutation_executes_only_after_matching_approval(
    approved, expected_calls, expected_code
) -> None:
    handler_calls = []
    approver = DecidingApprover(approved)

    def restart(arguments):
        handler_calls.append(arguments)
        return "restarted"

    executor = RegistryToolExecutor(
        ToolRegistry(
            (
                ToolDefinition(
                    "restart_service",
                    SCHEMA,
                    restart,
                    effect=ToolEffect.MUTATE,
                    requires_approval=True,
                    lane_argument="service_id",
                ),
            )
        ),
        approval_provider=approver,
    )

    observation = asyncio.run(
        executor.execute(
            ToolCall("restart-1", "restart_service", {"service_id": "payment"})
        )
    )

    assert len(handler_calls) == expected_calls
    assert approver.requests[0].lane == "service:payment"
    assert len(executor.approval_decisions) == 1
    if expected_code:
        assert f'"code":"{expected_code}"' in observation.content


def test_mutation_without_approval_provider_fails_closed() -> None:
    calls = []
    executor = RegistryToolExecutor(
        ToolRegistry(
            (
                ToolDefinition(
                    "rollback_service",
                    SCHEMA,
                    lambda arguments: calls.append(arguments),
                    effect="mutate",
                    requires_approval=True,
                ),
            )
        )
    )

    observation = asyncio.run(
        executor.execute(
            ToolCall("rollback-1", "rollback_service", {"service_id": "payment"})
        )
    )

    assert calls == []
    assert '"code":"approval_required"' in observation.content


def test_same_service_mutations_never_overlap_in_named_lane() -> None:
    active = 0
    maximum_active = 0

    async def mutate(_arguments):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.005)
        active -= 1
        return "ok"

    executor = RegistryToolExecutor(
        ToolRegistry(
            (
                ToolDefinition(
                    "restart_service",
                    SCHEMA,
                    mutate,
                    effect="mutate",
                    lane_argument="service_id",
                ),
            )
        ),
        approval_provider=ApproveAll(),
    )
    calls = tuple(
        ToolCall(f"call-{index}", "restart_service", {"service_id": "payment"})
        for index in range(4)
    )

    async def execute_all():
        return await asyncio.gather(*(executor.execute(call) for call in calls))

    asyncio.run(execute_all())

    assert maximum_active == 1


def test_batch_with_any_mutation_runs_entire_batch_in_original_order() -> None:
    events = []

    async def handle(arguments):
        events.append(f"start:{arguments['value']}")
        await asyncio.sleep(0)
        events.append(f"end:{arguments['value']}")
        return arguments["value"]

    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    executor = RegistryToolExecutor(
        ToolRegistry(
            (
                ToolDefinition("read", schema, handle),
                ToolDefinition("write", schema, handle, effect="mutate"),
            )
        ),
        approval_provider=ApproveAll(),
    )
    calls = (
        ToolCall("1", "read", {"value": "one"}),
        ToolCall("2", "write", {"value": "two"}),
        ToolCall("3", "read", {"value": "three"}),
    )

    observations = asyncio.run(executor.execute_batch(calls))

    assert events == [
        "start:one", "end:one",
        "start:two", "end:two",
        "start:three", "end:three",
    ]
    assert [item.call_id for item in observations] == ["1", "2", "3"]

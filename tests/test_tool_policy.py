from __future__ import annotations

import asyncio

import pytest

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import (
    PolicyAction,
    PolicyDecision,
    RegistryToolExecutor,
    ToolDefinition,
    ToolEffect,
    ToolRegistry,
)


class RecordingPolicy:
    def __init__(self, action, events):
        self.action = action
        self.events = events

    def evaluate(self, call, definition):
        self.events.append(f"policy:{definition.effect}")
        return PolicyDecision(self.action, "operator policy")


class RecordingHook:
    def __init__(self, name, events):
        self.name = name
        self.events = events

    def before_tool(self, call, definition):
        self.events.append(f"before:{self.name}")

    def after_tool(self, call, definition, observation):
        self.events.append(f"after:{self.name}")


@pytest.mark.parametrize(
    ("action", "expected_code", "handler_calls"),
    [
        (PolicyAction.ALLOW, None, 1),
        (PolicyAction.DENY, "policy_denied", 0),
        (PolicyAction.ASK, "approval_required", 0),
    ],
)
def test_policy_allow_deny_ask_branches(action, expected_code, handler_calls) -> None:
    calls = []
    events = []

    def handler(arguments):
        calls.append(arguments)
        return "ok"

    executor = RegistryToolExecutor(
        ToolRegistry(
            (
                ToolDefinition(
                    "query_service",
                    {"type": "object"},
                    handler,
                    effect=ToolEffect.READ,
                ),
            )
        ),
        policy=RecordingPolicy(action, events),
    )

    observation = asyncio.run(
        executor.execute(ToolCall("call-1", "query_service", {}))
    )

    assert len(calls) == handler_calls
    assert observation.is_error is (expected_code is not None)
    if expected_code:
        assert f'"code":"{expected_code}"' in observation.content


def test_hooks_have_stable_registration_order_around_policy_and_handler() -> None:
    events = []

    def handler(_arguments):
        events.append("handler")
        return "ok"

    executor = RegistryToolExecutor(
        ToolRegistry(
            (ToolDefinition("query_logs", {"type": "object"}, handler),)
        ),
        policy=RecordingPolicy(PolicyAction.ALLOW, events),
        hooks=(RecordingHook("one", events), RecordingHook("two", events)),
    )

    asyncio.run(executor.execute(ToolCall("call-1", "query_logs", {})))

    assert events == [
        "before:one",
        "before:two",
        "policy:read",
        "handler",
        "after:one",
        "after:two",
    ]

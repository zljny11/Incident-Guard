from __future__ import annotations

import asyncio

import pytest

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import (
    RegistryToolExecutor,
    ToolDefinition,
    ToolRegistry,
)


SCHEMA = {
    "type": "object",
    "properties": {"service_id": {"type": "string"}},
    "required": ["service_id"],
    "additionalProperties": False,
}


def run(coro):
    return asyncio.run(coro)


def test_known_tool_resolves_to_one_scoped_handler() -> None:
    handled = []
    def handle(arguments):
        handled.append(arguments)
        return ""

    registry = ToolRegistry(
        (ToolDefinition("query_health", SCHEMA, handle),)
    )
    executor = RegistryToolExecutor(registry)

    observation = run(
        executor.execute(
            ToolCall("call-1", "query_health", {"service_id": "payment"})
        )
    )

    assert handled == [{"service_id": "payment"}]
    assert observation.content == ""
    assert observation.is_error is False


@pytest.mark.parametrize(
    ("call", "error_code"),
    [
        (ToolCall("1", "missing", {}), "unknown_tool"),
        (ToolCall("2", "query_health", {}), "invalid_arguments"),
        (
            ToolCall("3", "query_health", {"service_id": 42}),
            "invalid_arguments",
        ),
    ],
)
def test_invalid_calls_return_stable_errors_without_invoking_handler(
    call, error_code
) -> None:
    handled = []
    def handle(arguments):
        handled.append(arguments)
        return ""

    registry = ToolRegistry(
        (ToolDefinition("query_health", SCHEMA, handle),)
    )

    observation = run(RegistryToolExecutor(registry).execute(call))

    assert handled == []
    assert observation.is_error is True
    assert f'"code":"{error_code}"' in observation.content


def test_registry_rejects_duplicate_names_and_invalid_schemas() -> None:
    definition = ToolDefinition("query_health", SCHEMA, lambda _: "healthy")
    registry = ToolRegistry((definition,))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(definition)
    with pytest.raises(ValueError, match="Invalid JSON Schema"):
        ToolDefinition("broken", {"type": "not-a-json-type"}, lambda _: "")


def test_handler_failure_is_normalized_without_leaking_exception_text() -> None:
    def fail(_arguments):
        raise RuntimeError("secret backend detail")

    executor = RegistryToolExecutor(
        ToolRegistry((ToolDefinition("query_health", SCHEMA, fail),))
    )

    observation = run(
        executor.execute(
            ToolCall("call-1", "query_health", {"service_id": "payment"})
        )
    )

    assert observation.is_error is True
    assert '"code":"execution_failed"' in observation.content
    assert "secret backend detail" not in observation.content

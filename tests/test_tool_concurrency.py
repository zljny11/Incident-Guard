from __future__ import annotations

import asyncio

from incident_guard.agents.provider import ToolCall
from incident_guard.agents.tool_pipeline import (
    RegistryToolExecutor,
    ToolDefinition,
    ToolRegistry,
)


def test_read_tools_have_bounded_concurrency_and_preserve_result_order() -> None:
    active = 0
    maximum_active = 0

    async def query(arguments):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(arguments["delay"])
        active -= 1
        return arguments["value"]

    executor = RegistryToolExecutor(
        ToolRegistry(
            (
                ToolDefinition(
                    "query_logs",
                    {
                        "type": "object",
                        "properties": {
                            "delay": {"type": "number"},
                            "value": {"type": "string"},
                        },
                        "required": ["delay", "value"],
                    },
                    query,
                ),
            )
        ),
        max_read_concurrency=4,
    )
    calls = tuple(
        ToolCall(
            f"call-{index}",
            "query_logs",
            {"delay": (8 - index) / 1000, "value": str(index)},
        )
        for index in range(8)
    )

    observations = asyncio.run(executor.execute_batch(calls))

    assert maximum_active == 4
    assert [item.call_id for item in observations] == [call.id for call in calls]
    assert [item.content for item in observations] == [str(index) for index in range(8)]


def test_runtime_uses_batch_execution_but_keeps_observation_order() -> None:
    from incident_guard.agents.provider import ProviderEvent, ProviderResponse
    from incident_guard.agents.react_runtime import StructuredAgentRuntime
    from incident_guard.agents.scripted_streaming_provider import ScriptedStreamingProvider

    calls = (
        ToolCall("slow", "query", {"delay": 0.01, "value": "first"}),
        ToolCall("fast", "query", {"delay": 0, "value": "second"}),
    )

    async def query(arguments):
        await asyncio.sleep(arguments["delay"])
        return arguments["value"]

    first = ProviderResponse(stop_reason="tool_use", tool_calls=calls)
    final = ProviderResponse(text="done")
    provider = ScriptedStreamingProvider(
        [
            [*(ProviderEvent.tool_call(call) for call in calls), ProviderEvent.completed(first)],
            [ProviderEvent.text_delta("done"), ProviderEvent.completed(final)],
        ]
    )
    executor = RegistryToolExecutor(
        ToolRegistry((ToolDefinition("query", {"type": "object"}, query),))
    )

    result = asyncio.run(
        StructuredAgentRuntime(provider, executor).run(
            "run-1", [{"role": "user", "content": "investigate"}]
        )
    )

    assert [item.content for item in result.turns[0].steps[0].observations] == [
        "first",
        "second",
    ]

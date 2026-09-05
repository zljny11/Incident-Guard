from __future__ import annotations

import asyncio

from incident_guard.agents.event_runtime import EventDrivenAgentRuntime
from incident_guard.agents.provider import ProviderEvent, ProviderResponse
from incident_guard.agents.react_runtime import FakeToolExecutor
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)
from incident_guard.events import LiveEventBroker, SQLiteEventStore


def test_text_delta_is_live_only_but_final_assistant_message_is_durable(
    tmp_path,
) -> None:
    async def scenario():
        store = SQLiteEventStore(tmp_path / "events.db")
        broker = LiveEventBroker()
        subscription = broker.subscribe("run-001")
        response = ProviderResponse(text="service healthy")
        provider = ScriptedStreamingProvider(
            [[
                ProviderEvent.text_delta("service "),
                ProviderEvent.text_delta("healthy"),
                ProviderEvent.completed(response),
            ]]
        )
        runtime = EventDrivenAgentRuntime(
            provider, FakeToolExecutor({}), store, live_events=broker
        )

        projection = await runtime.run(
            "run-001", [{"role": "user", "content": "status"}]
        )
        live = [event async for event in subscription]
        return store, projection, live

    store, projection, live = asyncio.run(scenario())

    assert [
        event.payload["text"]
        for event in live
        if event.event_type == "assistant.delta"
    ] == ["service ", "healthy"]
    durable_types = [event.event_type for event in store.replay("run-001")]
    assert "assistant.delta" not in durable_types
    assert durable_types.count("assistant.message") == 1
    assert projection.provider_messages[-1] == {
        "role": "assistant",
        "content": "service healthy",
    }

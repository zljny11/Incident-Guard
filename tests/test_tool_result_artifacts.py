from __future__ import annotations

import asyncio
import hashlib

import pytest

from incident_guard.agents.event_runtime import EventDrivenAgentRuntime
from incident_guard.agents.provider import (
    ProviderEvent,
    ProviderResponse,
    StopReason,
    ToolCall,
)
from incident_guard.agents.react_runtime import FakeToolExecutor
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)
from incident_guard.context import EventContextProjector, FileToolResultStore
from incident_guard.events import SQLiteEventStore


def events(response: ProviderResponse) -> list[ProviderEvent]:
    result = []
    if response.text:
        result.append(ProviderEvent.text_delta(response.text))
    result.extend(ProviderEvent.tool_call(call) for call in response.tool_calls)
    result.append(ProviderEvent.completed(response))
    return result


def test_large_tool_result_is_recoverable_but_context_only_contains_reference(
    tmp_path,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.db")
    artifacts = FileToolResultStore(
        tmp_path / "artifacts", threshold_bytes=64, preview_chars=24
    )
    call = ToolCall("logs-1", "query_logs", {})
    large_result = "log-line\n" * 100 + "SECRET_TAIL_MARKER"
    provider = ScriptedStreamingProvider(
        [
            events(
                ProviderResponse(
                    stop_reason=StopReason.TOOL_USE, tool_calls=(call,)
                )
            ),
            events(ProviderResponse(text="diagnosis complete")),
        ]
    )
    runtime = EventDrivenAgentRuntime(
        provider,
        FakeToolExecutor({"logs-1": large_result}),
        store,
        tool_result_store=artifacts,
    )

    result = asyncio.run(
        runtime.run("run-001", [{"role": "user", "content": "investigate"}])
    )

    completed = next(
        event
        for event in store.replay("run-001")
        if event.event_type == "tool.completed"
    )
    reference = completed.payload["content_ref"]
    digest = hashlib.sha256(large_result.encode()).hexdigest()
    assert completed.payload["content_sha256"] == digest
    assert completed.payload["content_externalized"] is True
    assert "SECRET_TAIL_MARKER" not in completed.payload["content"]
    assert artifacts.load(reference, expected_sha256=digest) == large_result

    snapshot = EventContextProjector().project(
        "run-001", store.replay("run-001")
    )
    provider_context = str(snapshot.to_provider_messages())
    assert reference in provider_context
    assert digest in provider_context
    assert "SECRET_TAIL_MARKER" not in provider_context
    assert result.status.value == "completed"


def test_small_tool_result_stays_inline_without_artifact_file(tmp_path) -> None:
    artifacts = FileToolResultStore(
        tmp_path / "artifacts", threshold_bytes=64, preview_chars=8
    )

    stored = artifacts.store("healthy")

    assert stored.context_content == "healthy"
    assert stored.reference is None
    assert stored.externalized is False
    assert list((tmp_path / "artifacts").rglob("*.txt")) == []


def test_artifact_load_rejects_escape_and_hash_mismatch(tmp_path) -> None:
    artifacts = FileToolResultStore(
        tmp_path / "artifacts", threshold_bytes=1, preview_chars=1
    )
    stored = artifacts.store("large result")
    assert stored.reference is not None

    with pytest.raises(ValueError, match="relative and contained"):
        artifacts.load("../secret")
    with pytest.raises(ValueError, match="verification failed"):
        artifacts.load(stored.reference, expected_sha256="0" * 64)

from __future__ import annotations

import json

from incident_guard.agents.agent_runtime import AgentRuntime
from incident_guard.agents.provider import FakeProvider
from incident_guard.gateway.inbound_message import InboundMessage
from incident_guard.gateway.runtime import GatewayRuntime
from incident_guard.observability.trace_logger import TraceLogger
from incident_guard.sessions.session_store import SessionStore


def test_session_store_appends_and_replays_jsonl(tmp_path) -> None:
    store = SessionStore(base_dir=tmp_path)
    session_id = "demo-account__web__userA__incident-agent"

    store.append_message(session_id, "user", "帮我看看这个 bug")
    store.append_message(
        session_id,
        "assistant",
        "[fake-agent-response] I received: 帮我看看这个 bug",
    )

    history = store.replay(session_id)

    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[0]["content"] == "帮我看看这个 bug"
    assert history[1]["content"].startswith("[fake-agent-response]")


def test_agent_runtime_returns_fake_provider_response() -> None:
    history = [{"role": "user", "content": "hello", "metadata": {}}]

    response_text = AgentRuntime(provider=FakeProvider()).run(history)

    assert response_text == "[fake-agent-response] I received: hello"


def test_gateway_runtime_handles_message_and_returns_result(tmp_path) -> None:
    session_store = SessionStore(base_dir=tmp_path / "sessions")
    trace_logger = TraceLogger(base_dir=tmp_path / "traces")
    runtime = GatewayRuntime(
        session_store=session_store,
        trace_logger=trace_logger,
    )
    message = InboundMessage.create(
        channel="web",
        account_id="demo-account",
        peer_id="userA",
        text="帮我看看这个 bug",
    )

    result = runtime.handle_message(message)

    assert result.message_id == message.message_id
    assert result.agent_id == "incident-agent"
    assert result.reason == "selected single incident-agent profile"
    assert result.session_id == "demo-account__web__userA__incident-agent"
    assert result.response_text == "[fake-agent-response] I received: 帮我看看这个 bug"
    assert result.trace_path == trace_logger.trace_path


def test_gateway_runtime_persists_user_and_assistant_messages(tmp_path) -> None:
    session_store = SessionStore(base_dir=tmp_path / "sessions")
    runtime = GatewayRuntime(
        session_store=session_store,
        trace_logger=TraceLogger(base_dir=tmp_path / "traces"),
    )
    message = InboundMessage.create(
        channel="web",
        account_id="demo-account",
        peer_id="userA",
        text="帮我看看这个 bug",
    )

    result = runtime.handle_message(message)
    history = session_store.replay(result.session_id)

    assert [record["role"] for record in history] == ["user", "assistant"]
    assert history[0]["content"] == "帮我看看这个 bug"
    assert history[0]["metadata"] == {"message_id": message.message_id}
    assert history[1]["content"] == result.response_text


def test_gateway_runtime_writes_required_trace_events(tmp_path) -> None:
    trace_logger = TraceLogger(base_dir=tmp_path / "traces")
    runtime = GatewayRuntime(
        session_store=SessionStore(base_dir=tmp_path / "sessions"),
        trace_logger=trace_logger,
    )
    message = InboundMessage.create(
        channel="web",
        account_id="demo-account",
        peer_id="userA",
        text="帮我看看这个 bug",
    )

    runtime.handle_message(message)
    records = [
        json.loads(line)
        for line in trace_logger.trace_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [record["event_type"] for record in records] == [
        "message_received",
        "agent_selected",
        "session_replayed",
        "agent_response_generated",
    ]


def test_trace_logger_writes_required_event_shape(tmp_path) -> None:
    logger = TraceLogger(base_dir=tmp_path)

    logger.log(
        "agent_selected",
        session_id="demo-account__web__userA__incident-agent",
        metadata={"agent_id": "incident-agent"},
    )

    records = [
        json.loads(line)
        for line in logger.trace_path.read_text(encoding="utf-8").splitlines()
    ]

    assert records == [
        {
            "trace_id": logger.trace_id,
            "event_type": "agent_selected",
            "status": "success",
            "session_id": "demo-account__web__userA__incident-agent",
            "timestamp": records[0]["timestamp"],
            "metadata": {"agent_id": "incident-agent"},
            "error": None,
        }
    ]

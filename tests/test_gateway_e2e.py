from __future__ import annotations

import json

from incident_guard.agents.agent_runtime import AgentRuntime
from incident_guard.agents.provider import FakeProvider
from incident_guard.channels.cli_adapter import CliChannelAdapter
from incident_guard.gateway.runtime import GatewayRuntime
from incident_guard.observability.trace_logger import TraceLogger
from incident_guard.sessions.session_store import SessionStore


def test_cli_event_reaches_route_session_provider_and_trace(tmp_path) -> None:
    """Exercise the complete offline Gateway baseline through public seams."""

    session_store = SessionStore(base_dir=tmp_path / "sessions")
    trace_logger = TraceLogger(base_dir=tmp_path / "traces")
    runtime = GatewayRuntime(
        session_store=session_store,
        agent_runtime=AgentRuntime(provider=FakeProvider()),
        trace_logger=trace_logger,
    )
    message = CliChannelAdapter().to_inbound_message(
        {
            "account_id": "demo-account",
            "peer_id": "operator-1",
            "text": "检查 payment-service",
            "metadata": {"terminal": "test"},
        }
    )

    result = runtime.handle_message(message)

    assert result.agent_id == "incident-agent"
    assert result.reason == "selected single incident-agent profile"
    assert result.session_id == (
        "demo-account__cli__operator-1__incident-agent"
    )
    assert result.response_text == (
        "[fake-agent-response] I received: 检查 payment-service"
    )

    history = session_store.replay(result.session_id)
    assert [record["role"] for record in history] == ["user", "assistant"]
    assert history[0]["content"] == "检查 payment-service"
    assert history[0]["metadata"] == {"message_id": message.message_id}
    assert history[1]["content"] == result.response_text

    trace = [
        json.loads(line)
        for line in result.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["event_type"] for record in trace] == [
        "message_received",
        "agent_selected",
        "session_replayed",
        "agent_response_generated",
    ]
    assert trace[1]["session_id"] == result.session_id
    assert trace[1]["metadata"] == {
        "agent_id": "incident-agent",
        "reason": "selected single incident-agent profile",
    }
    assert trace[2]["metadata"] == {"message_count": 1}

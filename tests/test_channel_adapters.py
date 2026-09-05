from __future__ import annotations

import pytest

from incident_guard.channels.cli_adapter import CliChannelAdapter
from incident_guard.channels.mock_webhook_adapter import MockWebhookAdapter
from incident_guard.gateway.runtime import GatewayRuntime
from incident_guard.observability.trace_logger import TraceLogger
from incident_guard.sessions.session_store import SessionStore


def test_cli_adapter_maps_raw_event_to_inbound_message() -> None:
    raw_event = {
        "account_id": "demo-account",
        "peer_id": "userA",
        "text": "帮我看看这个 CLI bug",
        "metadata": {"terminal": "ubuntu"},
    }

    message = CliChannelAdapter().to_inbound_message(raw_event)

    assert message.channel == "cli"
    assert message.account_id == "demo-account"
    assert message.peer_id == "userA"
    assert message.text == "帮我看看这个 CLI bug"
    assert message.metadata == {"source": "cli", "terminal": "ubuntu"}


def test_mock_webhook_adapter_maps_raw_event_to_inbound_message() -> None:
    raw_event = {
        "account": "demo-account",
        "user": "userA",
        "message": "帮我看看这个 Web bug",
        "request_id": "web-001",
    }

    message = MockWebhookAdapter().to_inbound_message(raw_event)

    assert message.channel == "web"
    assert message.account_id == "demo-account"
    assert message.peer_id == "userA"
    assert message.text == "帮我看看这个 Web bug"
    assert message.metadata == {"source": "mock-web", "request_id": "web-001"}


def test_cli_adapter_requires_account_peer_and_text() -> None:
    adapter = CliChannelAdapter()

    with pytest.raises(ValueError, match="account_id"):
        adapter.to_inbound_message({"peer_id": "userA", "text": "hello"})

    with pytest.raises(ValueError, match="peer_id"):
        adapter.to_inbound_message({"account_id": "demo-account", "text": "hello"})

    with pytest.raises(ValueError, match="text"):
        adapter.to_inbound_message({"account_id": "demo-account", "peer_id": "userA"})


def test_mock_webhook_adapter_requires_account_user_and_message() -> None:
    adapter = MockWebhookAdapter()

    with pytest.raises(ValueError, match="account"):
        adapter.to_inbound_message({"user": "userA", "message": "hello"})

    with pytest.raises(ValueError, match="user"):
        adapter.to_inbound_message({"account": "demo-account", "message": "hello"})

    with pytest.raises(ValueError, match="message"):
        adapter.to_inbound_message({"account": "demo-account", "user": "userA"})


def test_channel_adapters_feed_the_same_gateway_runtime(tmp_path) -> None:
    runtime = GatewayRuntime(
        session_store=SessionStore(base_dir=tmp_path / "sessions"),
        trace_logger=TraceLogger(base_dir=tmp_path / "traces"),
    )
    cli_message = CliChannelAdapter().to_inbound_message(
        {
            "account_id": "demo-account",
            "peer_id": "userA",
            "text": "CLI 输入",
        }
    )
    web_message = MockWebhookAdapter().to_inbound_message(
        {
            "account": "demo-account",
            "user": "userA",
            "message": "Web 输入",
        }
    )

    cli_result = runtime.handle_message(cli_message)
    web_result = runtime.handle_message(web_message)

    assert cli_result.agent_id == "incident-agent"
    assert cli_result.response_text == "[fake-agent-response] I received: CLI 输入"
    assert web_result.agent_id == "incident-agent"
    assert web_result.response_text == "[fake-agent-response] I received: Web 输入"

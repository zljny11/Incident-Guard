from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

import incident_guard.agents.openai_compatible_provider as provider_module
from incident_guard.agents.agent_runtime import AgentRuntime
from incident_guard.agents.openai_compatible_provider import OpenAICompatibleProvider
from incident_guard.agents.provider import (
    ProviderEventType,
    ProviderError,
    ProviderResponse,
    ProviderUsage,
    StopReason,
    ToolCall,
)
from incident_guard.gateway.inbound_message import InboundMessage
from incident_guard.gateway.runtime import GatewayRuntime
from incident_guard.observability.trace_logger import TraceLogger
from incident_guard.sessions.session_store import SessionStore


def test_openai_provider_sends_normalized_chat_completion_request() -> None:
    captured: dict = {}

    def transport(request, timeout_seconds: float) -> bytes:
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["content_type"] = request.get_header("Content-type")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout_seconds"] = timeout_seconds
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": "real provider response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 6, "completion_tokens": 3},
            }
        ).encode("utf-8")

    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        base_url="https://llm.example/v1/",
        timeout_seconds=12.5,
        transport=transport,
    )
    response = provider.generate(
        [
            {
                "role": "user",
                "content": "hello",
                "timestamp": 123,
                "metadata": {"message_id": "message-1"},
            }
        ]
    )

    assert captured == {
        "url": "https://llm.example/v1/chat/completions",
        "authorization": "Bearer test-key",
        "content_type": "application/json",
        "body": {
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        },
        "timeout_seconds": 12.5,
    }
    assert response.text == "real provider response"
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage == ProviderUsage(input_tokens=6, output_tokens=3)


def test_openai_provider_normalizes_length_stop_reason() -> None:
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        transport=lambda request, timeout: json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": "partial response"},
                        "finish_reason": "length",
                    }
                ]
            }
        ).encode("utf-8"),
    )

    response = provider.generate([{"role": "user", "content": "hello"}])

    assert response.stop_reason is StopReason.MAX_TOKENS


def test_openai_provider_sends_tools_and_parses_tool_calls() -> None:
    captured: dict = {}

    def transport(request, _timeout) -> bytes:
        captured.update(json.loads(request.data.decode("utf-8")))
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "query_service_health",
                                        "arguments": '{"service_id":"payment-service"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        ).encode()

    tool = {
        "type": "function",
        "function": {
            "name": "query_service_health",
            "description": "Query health",
            "parameters": {"type": "object"},
        },
    }
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        tools=(tool,),
        temperature=0,
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "disabled"}},
        transport=transport,
    )

    response = provider.generate([{"role": "user", "content": "alert"}])

    assert captured["tools"] == [tool]
    assert captured["temperature"] == 0
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["thinking"] == {"type": "disabled"}
    assert response == ProviderResponse(
        stop_reason=StopReason.TOOL_USE,
        tool_calls=(
            ToolCall(
                "call-1",
                "query_service_health",
                {"service_id": "payment-service"},
            ),
        ),
        usage=ProviderUsage(10, 4),
    )


def test_openai_provider_round_trips_tool_context_and_streams_events() -> None:
    captured: dict = {}

    def transport(request, _timeout) -> bytes:
        captured.update(json.loads(request.data.decode("utf-8")))
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": "done"},
                        "finish_reason": "stop",
                    }
                ]
            }
        ).encode()

    provider = OpenAICompatibleProvider(
        api_key="test-key", model="test-model", transport=transport
    )
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "name": "query_service_health",
                    "arguments": {"service_id": "payment-service"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "query_service_health",
            "content": '{"status":"unhealthy"}',
            "is_error": False,
        },
    ]

    async def collect():
        return [event async for event in provider.stream(messages)]

    import asyncio

    events = asyncio.run(collect())

    assert captured["messages"] == [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "query_service_health",
                        "arguments": '{"service_id": "payment-service"}',
                    },
                }
            ],
        },
        {"role": "tool", "content": '{"status":"unhealthy"}', "tool_call_id": "call-1"},
    ]
    assert [event.event_type for event in events] == [
        ProviderEventType.TEXT_DELTA,
        ProviderEventType.COMPLETED,
    ]
    assert events[-1].response == ProviderResponse(text="done")


def test_openai_provider_rejects_invalid_message_shape() -> None:
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        transport=lambda request, timeout: b"{}",
    )

    with pytest.raises(ProviderError, match="requires string role and content"):
        provider.generate([{"role": "user"}])


def test_openai_provider_rejects_invalid_response_shape() -> None:
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        transport=lambda request, timeout: b'{"choices": []}',
    )

    with pytest.raises(ProviderError, match="invalid response shape"):
        provider.generate([{"role": "user", "content": "hello"}])


def test_openai_provider_converts_http_error(monkeypatch) -> None:
    def failing_urlopen(request, timeout):
        raise HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            hdrs=None,
            fp=BytesIO(b'{"error":{"message":"rate limit reached"}}'),
        )

    monkeypatch.setattr(provider_module, "urlopen", failing_urlopen)
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
    )

    with pytest.raises(
        ProviderError,
        match="HTTP 429: rate limit reached",
    ):
        provider.generate([{"role": "user", "content": "hello"}])


class FailingProvider:
    def generate(self, messages: list[dict]) -> ProviderResponse:
        raise ProviderError("upstream unavailable")


def test_gateway_records_provider_error_trace(tmp_path) -> None:
    trace_logger = TraceLogger(base_dir=tmp_path / "traces")
    runtime = GatewayRuntime(
        session_store=SessionStore(base_dir=tmp_path / "sessions"),
        agent_runtime=AgentRuntime(provider=FailingProvider()),
        trace_logger=trace_logger,
    )
    message = InboundMessage.create(
        channel="web",
        account_id="demo-account",
        peer_id="userA",
        text="hello",
    )

    with pytest.raises(ProviderError, match="upstream unavailable"):
        runtime.handle_message(message)

    records = [
        json.loads(line)
        for line in trace_logger.trace_path.read_text(encoding="utf-8").splitlines()
    ]
    provider_error = records[-1]
    assert provider_error["event_type"] == "provider_error"
    assert provider_error["status"] == "error"
    assert provider_error["error"] == "upstream unavailable"

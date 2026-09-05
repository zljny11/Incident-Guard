from __future__ import annotations

import pytest

from incident_guard.agents.provider import (
    ProviderEvent,
    ProviderEventType,
    ProviderResponse,
    ProviderUsage,
    StopReason,
    ToolCall,
)


def test_tool_call_represents_structured_provider_request() -> None:
    call = ToolCall(
        id="call-1",
        name="query_service_health",
        arguments={"service_id": "payment-service"},
    )

    assert call.id == "call-1"
    assert call.name == "query_service_health"
    assert call.arguments == {"service_id": "payment-service"}


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"id": "", "name": "query_logs", "arguments": {}}, "id"),
        ({"id": "call-1", "name": "", "arguments": {}}, "name"),
        (
            {"id": "call-1", "name": "query_logs", "arguments": "{}"},
            "arguments",
        ),
    ],
)
def test_tool_call_rejects_invalid_shape(kwargs, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        ToolCall(**kwargs)


def test_provider_usage_reports_total_tokens() -> None:
    usage = ProviderUsage(input_tokens=12, output_tokens=5)

    assert usage.total_tokens == 17


@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_provider_usage_rejects_invalid_token_count(value) -> None:
    with pytest.raises(ValueError, match="input_tokens"):
        ProviderUsage(input_tokens=value, output_tokens=0)


def test_provider_response_preserves_text_response_compatibility() -> None:
    response = ProviderResponse(text="service is healthy", stop_reason="end_turn")

    assert response.text == "service is healthy"
    assert response.stop_reason is StopReason.END_TURN
    assert response.tool_calls == ()
    assert response.usage is None


def test_provider_response_accepts_structured_tool_calls_and_usage() -> None:
    call = ToolCall("call-1", "query_logs", {"service_id": "payment-service"})
    usage = ProviderUsage(input_tokens=20, output_tokens=8)

    response = ProviderResponse(
        stop_reason=StopReason.TOOL_USE,
        tool_calls=[call],
        usage=usage,
    )

    assert response.text == ""
    assert response.stop_reason is StopReason.TOOL_USE
    assert response.tool_calls == (call,)
    assert response.usage is usage


def test_provider_response_rejects_tool_use_without_calls() -> None:
    with pytest.raises(ValueError, match="requires at least one ToolCall"):
        ProviderResponse(stop_reason=StopReason.TOOL_USE)


def test_provider_response_rejects_calls_without_tool_use() -> None:
    call = ToolCall("call-1", "query_logs", {})

    with pytest.raises(ValueError, match="require the tool_use stop reason"):
        ProviderResponse(stop_reason=StopReason.END_TURN, tool_calls=(call,))


def test_provider_response_rejects_duplicate_tool_call_ids() -> None:
    calls = (
        ToolCall("call-1", "query_logs", {}),
        ToolCall("call-1", "query_metrics", {}),
    )

    with pytest.raises(ValueError, match="ids must be unique"):
        ProviderResponse(stop_reason=StopReason.TOOL_USE, tool_calls=calls)


def test_provider_response_rejects_unknown_stop_reason() -> None:
    with pytest.raises(ValueError, match="Unsupported.*stop_reason"):
        ProviderResponse(text="partial", stop_reason="unknown")


def test_provider_events_represent_stream_payloads() -> None:
    call = ToolCall("call-1", "query_metrics", {"service_id": "payment-service"})
    response = ProviderResponse(
        stop_reason=StopReason.TOOL_USE,
        tool_calls=(call,),
    )

    text_event = ProviderEvent.text_delta("checking")
    tool_event = ProviderEvent.tool_call(call)
    completed_event = ProviderEvent.completed(response)

    assert text_event == ProviderEvent(
        event_type=ProviderEventType.TEXT_DELTA,
        text="checking",
    )
    assert tool_event.call is call
    assert completed_event.response is response


@pytest.mark.parametrize(
    "event",
    [
        lambda: ProviderEvent(event_type=ProviderEventType.TEXT_DELTA),
        lambda: ProviderEvent(
            event_type=ProviderEventType.TEXT_DELTA,
            text="delta",
            call=ToolCall("call-1", "query_logs", {}),
        ),
        lambda: ProviderEvent(
            event_type=ProviderEventType.TOOL_CALL,
            text="not-a-tool-call",
        ),
    ],
)
def test_provider_event_rejects_invalid_payload_combinations(event) -> None:
    with pytest.raises(ValueError):
        event()

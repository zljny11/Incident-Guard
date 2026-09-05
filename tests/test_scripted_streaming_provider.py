from __future__ import annotations

import asyncio

import pytest

from incident_guard.agents.provider import (
    ProviderError,
    ProviderEvent,
    ProviderResponse,
    ProviderUsage,
    StopReason,
    ToolCall,
)
from incident_guard.agents.scripted_streaming_provider import (
    ScriptedStreamingProvider,
)


async def collect_stream(provider, messages=None):
    return [event async for event in provider.stream(messages or [])]


def test_stream_emits_text_deltas_and_matching_completion_in_order() -> None:
    response = ProviderResponse(
        text="service healthy",
        usage=ProviderUsage(input_tokens=8, output_tokens=2),
    )
    script = [
        ProviderEvent.text_delta("service "),
        ProviderEvent.text_delta("healthy"),
        ProviderEvent.completed(response),
    ]
    provider = ScriptedStreamingProvider([script])

    events = asyncio.run(
        collect_stream(provider, [{"role": "user", "content": "status?"}])
    )

    assert events == script
    assert "".join(event.text or "" for event in events) == response.text
    assert provider.requests == [({"role": "user", "content": "status?"},)]


def test_stream_emits_tool_calls_and_matching_completion() -> None:
    calls = (
        ToolCall("call-1", "query_service_health", {"service_id": "payment"}),
        ToolCall("call-2", "query_metrics", {"service_id": "payment"}),
    )
    response = ProviderResponse(
        text="checking",
        stop_reason=StopReason.TOOL_USE,
        tool_calls=calls,
    )
    provider = ScriptedStreamingProvider(
        [[
            ProviderEvent.text_delta("check"),
            ProviderEvent.text_delta("ing"),
            *(ProviderEvent.tool_call(call) for call in calls),
            ProviderEvent.completed(response),
        ]]
    )

    events = asyncio.run(collect_stream(provider))

    assert [event.call for event in events if event.call is not None] == list(calls)
    assert events[-1].response is response


def test_each_stream_call_consumes_the_next_script() -> None:
    first = ProviderResponse(text="first")
    second = ProviderResponse(text="second")
    provider = ScriptedStreamingProvider(
        [
            [ProviderEvent.text_delta("first"), ProviderEvent.completed(first)],
            [ProviderEvent.text_delta("second"), ProviderEvent.completed(second)],
        ]
    )

    assert asyncio.run(collect_stream(provider))[-1].response is first
    assert asyncio.run(collect_stream(provider))[-1].response is second

    with pytest.raises(ProviderError, match="no remaining stream"):
        asyncio.run(collect_stream(provider))


def test_scripted_error_is_raised_after_prior_deltas() -> None:
    provider = ScriptedStreamingProvider(
        [[ProviderEvent.text_delta("partial"), ProviderError("upstream failed")]]
    )

    async def consume() -> list[ProviderEvent]:
        observed = []
        with pytest.raises(ProviderError, match="upstream failed"):
            async for event in provider.stream([]):
                observed.append(event)
        return observed

    observed = asyncio.run(consume())

    assert observed == [ProviderEvent.text_delta("partial")]


def test_stream_without_completed_event_has_stable_interruption_error() -> None:
    provider = ScriptedStreamingProvider([[ProviderEvent.text_delta("partial")]])

    with pytest.raises(ProviderError, match="interrupted before completed"):
        asyncio.run(collect_stream(provider))


@pytest.mark.parametrize(
    ("script", "error"),
    [
        (
            [
                ProviderEvent.text_delta("partial"),
                ProviderEvent.completed(ProviderResponse(text="different")),
            ],
            "text does not match",
        ),
        (
            [
                ProviderEvent.tool_call(ToolCall("call-1", "query_logs", {})),
                ProviderEvent.completed(
                    ProviderResponse(
                        stop_reason=StopReason.TOOL_USE,
                        tool_calls=(ToolCall("call-2", "query_metrics", {}),),
                    )
                ),
            ],
            "tool calls do not match",
        ),
    ],
)
def test_completed_event_must_match_streamed_content(script, error: str) -> None:
    provider = ScriptedStreamingProvider([script])

    with pytest.raises(ProviderError, match=error):
        asyncio.run(collect_stream(provider))

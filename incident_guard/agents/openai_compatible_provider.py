from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from incident_guard.agents.provider import (
    ProviderError,
    ProviderResponse,
    ProviderUsage,
    StopReason,
    ToolCall,
)


HttpTransport = Callable[[Request, float], bytes]


def _default_http_transport(request: Request, timeout_seconds: float) -> bytes:
    """发送 HTTP 请求，并把底层网络异常转换为稳定的 ProviderError。"""

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.read()
    except HTTPError as error:
        detail = _read_api_error(error.read())
        raise ProviderError(
            f"Provider request failed with HTTP {error.code}: {detail}"
        ) from error
    except URLError as error:
        raise ProviderError(f"Provider request failed: {error.reason}") from error
    except TimeoutError as error:
        raise ProviderError("Provider request timed out") from error
    except ValueError as error:
        raise ProviderError(f"Provider request is invalid: {error}") from error


def _read_api_error(raw_body: bytes) -> str:
    """尽量从兼容 API 的错误响应中提取可读信息。"""

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "unreadable error response"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return "unknown API error"


class OpenAICompatibleProvider:
    """通过 OpenAI-compatible Chat Completions HTTP API 生成回复。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
        transport: HttpTransport = _default_http_transport,
        tools: tuple[Mapping[str, Any], ...] = (),
        temperature: float | None = None,
        response_format: Mapping[str, Any] | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self.tools = tuple(dict(tool) for tool in tools)
        self.temperature = temperature
        self.response_format = (
            dict(response_format) if response_format is not None else None
        )
        self.extra_body = dict(extra_body) if extra_body is not None else {}
        protected = {"model", "messages", "tools"}.intersection(self.extra_body)
        if protected:
            raise ValueError(
                f"extra_body cannot override protected fields: {sorted(protected)}"
            )

    def generate(self, messages: list[dict]) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": self._normalize_messages(messages),
        }
        if self.tools:
            payload["tools"] = list(self.tools)
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.response_format is not None:
            payload["response_format"] = self.response_format
        payload.update(self.extra_body)
        request = Request(
            url=f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        raw_response = self.transport(request, self.timeout_seconds)
        return self._parse_response(raw_response)

    async def stream(
        self, messages: list[dict]
    ) -> AsyncIterator["ProviderEvent"]:
        """Expose a streaming-compatible contract around one Chat Completions call.

        Network I/O runs in a worker thread so the async Runtime remains responsive.
        Durable text and tool-call events are emitted in the same order as the final
        response even when an OpenAI-compatible endpoint returns a non-streamed body.
        """

        from incident_guard.agents.provider import ProviderEvent

        if self.transport is _default_http_transport:
            response = await asyncio.to_thread(self.generate, messages)
        else:
            # Injected transports are deterministic test adapters and must not
            # consume a worker thread in restricted test sandboxes.
            response = self.generate(messages)
        if response.text:
            yield ProviderEvent.text_delta(response.text)
        for call in response.tool_calls:
            yield ProviderEvent.tool_call(call)
        yield ProviderEvent.completed(response)

    @staticmethod
    def _normalize_messages(messages: list[dict]) -> list[dict[str, Any]]:
        """只发送 provider API 需要的字段，排除 session 内部 metadata。"""

        normalized = []
        for index, message in enumerate(messages):
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str) or not isinstance(content, str):
                raise ProviderError(
                    f"Provider message at index {index} requires string role and content"
                )
            item: dict[str, Any] = {"role": role, "content": content}
            if role == "assistant" and message.get("tool_calls"):
                tool_calls = message["tool_calls"]
                if not isinstance(tool_calls, list):
                    raise ProviderError(
                        f"Provider message at index {index} tool_calls must be a list"
                    )
                item["tool_calls"] = [
                    OpenAICompatibleProvider._normalize_tool_call(call, index)
                    for call in tool_calls
                ]
            if role == "tool":
                tool_call_id = message.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    raise ProviderError(
                        f"Provider tool message at index {index} requires tool_call_id"
                    )
                item["tool_call_id"] = tool_call_id
            normalized.append(item)
        return normalized

    @staticmethod
    def _normalize_tool_call(call: object, message_index: int) -> dict[str, Any]:
        if not isinstance(call, Mapping):
            raise ProviderError(
                f"Provider message at index {message_index} has invalid tool call"
            )
        call_id = call.get("id")
        name = call.get("name")
        arguments = call.get("arguments")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
            or not isinstance(arguments, Mapping)
        ):
            raise ProviderError(
                f"Provider message at index {message_index} has invalid tool call"
            )
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(
                    dict(arguments), ensure_ascii=False, sort_keys=True
                ),
            },
        }

    @staticmethod
    def _parse_response(raw_response: bytes) -> ProviderResponse:
        try:
            payload: Any = json.loads(raw_response.decode("utf-8"))
            choice = payload["choices"][0]
            message = choice["message"]
            text = message.get("content") or ""
            finish_reason = choice.get("finish_reason")
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
        ) as error:
            raise ProviderError("Provider returned an invalid response shape") from error

        if not isinstance(text, str):
            raise ProviderError("Provider response content must be a string")

        stop_reason_by_finish_reason = {
            "stop": StopReason.END_TURN,
            "length": StopReason.MAX_TOKENS,
            "tool_calls": StopReason.TOOL_USE,
        }
        stop_reason = stop_reason_by_finish_reason.get(finish_reason)
        if stop_reason is None:
            raise ProviderError(
                f"Provider returned unsupported finish_reason: {finish_reason}"
            )

        tool_calls: tuple[ToolCall, ...] = ()
        if finish_reason == "tool_calls":
            try:
                tool_calls = tuple(
                    OpenAICompatibleProvider._parse_tool_call(item)
                    for item in message["tool_calls"]
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ProviderError("Provider returned invalid tool calls") from error

        usage_payload = payload.get("usage")
        usage = None
        if usage_payload is not None:
            if not isinstance(usage_payload, dict):
                raise ProviderError("Provider response usage must be an object")
            try:
                usage = ProviderUsage(
                    input_tokens=usage_payload["prompt_tokens"],
                    output_tokens=usage_payload["completion_tokens"],
                )
            except (KeyError, ValueError) as error:
                raise ProviderError("Provider returned invalid usage") from error

        return ProviderResponse(
            text=text,
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            usage=usage,
        )

    @staticmethod
    def _parse_tool_call(value: object) -> ToolCall:
        if not isinstance(value, Mapping) or value.get("type", "function") != "function":
            raise ValueError("tool call must be a function")
        function = value["function"]
        if not isinstance(function, Mapping):
            raise TypeError("tool call function must be an object")
        arguments = json.loads(function["arguments"])
        if not isinstance(arguments, dict):
            raise ValueError("tool call arguments must decode to an object")
        return ToolCall(
            id=value["id"],
            name=function["name"],
            arguments=arguments,
        )

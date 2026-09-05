from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from collections.abc import AsyncIterator
from typing import Any, Protocol


class StopReason(StrEnum):
    """Provider 完成一次生成的标准原因。"""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"


@dataclass(frozen=True, slots=True)
class ToolCall:
    """模型请求 Harness 执行的一次结构化工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("ToolCall id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("ToolCall name must be a non-empty string")
        if not isinstance(self.arguments, dict):
            raise ValueError("ToolCall arguments must be a dict")


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """一次 Provider 请求的标准 token 用量。"""

    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(
                    f"ProviderUsage {field_name} must be a non-negative int"
                )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """ProviderResponse 是 provider 返回给 AgentRuntime 的标准结果。"""

    text: str = ""
    stop_reason: StopReason = StopReason.END_TURN
    tool_calls: tuple[ToolCall, ...] = ()
    usage: ProviderUsage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("ProviderResponse text must be a string")

        try:
            normalized_reason = StopReason(self.stop_reason)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Unsupported ProviderResponse stop_reason: {self.stop_reason}"
            ) from error
        object.__setattr__(self, "stop_reason", normalized_reason)

        if isinstance(self.tool_calls, list):
            object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not isinstance(self.tool_calls, tuple) or not all(
            isinstance(call, ToolCall) for call in self.tool_calls
        ):
            raise ValueError("ProviderResponse tool_calls must contain ToolCall values")
        if len({call.id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("ProviderResponse tool call ids must be unique")

        if normalized_reason is StopReason.TOOL_USE and not self.tool_calls:
            raise ValueError("tool_use stop reason requires at least one ToolCall")
        if normalized_reason is not StopReason.TOOL_USE and self.tool_calls:
            raise ValueError("ToolCall values require the tool_use stop reason")
        if self.usage is not None and not isinstance(self.usage, ProviderUsage):
            raise ValueError("ProviderResponse usage must be ProviderUsage or None")


class ProviderEventType(StrEnum):
    """流式 Provider 对外暴露的标准事件类型。"""

    TEXT_DELTA = "text_delta"
    TOOL_CALL = "tool_call"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """一条流式 Provider 事件；每种事件只允许对应的 payload。"""

    event_type: ProviderEventType
    text: str | None = None
    call: ToolCall | None = None
    response: ProviderResponse | None = None

    def __post_init__(self) -> None:
        try:
            normalized_type = ProviderEventType(self.event_type)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Unsupported ProviderEvent event_type: {self.event_type}"
            ) from error
        object.__setattr__(self, "event_type", normalized_type)

        populated_payloads = sum(
            value is not None for value in (self.text, self.call, self.response)
        )
        if populated_payloads != 1:
            raise ValueError("ProviderEvent requires exactly one payload")
        if normalized_type is ProviderEventType.TEXT_DELTA:
            if not isinstance(self.text, str) or not self.text:
                raise ValueError("text_delta event requires non-empty text")
        elif normalized_type is ProviderEventType.TOOL_CALL:
            if not isinstance(self.call, ToolCall):
                raise ValueError("tool_call event requires a ToolCall")
        elif normalized_type is ProviderEventType.COMPLETED:
            if not isinstance(self.response, ProviderResponse):
                raise ValueError("completed event requires a ProviderResponse")

    @classmethod
    def text_delta(cls, text: str) -> "ProviderEvent":
        return cls(event_type=ProviderEventType.TEXT_DELTA, text=text)

    @classmethod
    def tool_call(cls, call: ToolCall) -> "ProviderEvent":
        return cls(event_type=ProviderEventType.TOOL_CALL, call=call)

    @classmethod
    def completed(cls, response: ProviderResponse) -> "ProviderEvent":
        return cls(event_type=ProviderEventType.COMPLETED, response=response)


class ProviderError(RuntimeError):
    """ProviderError 表示 provider 配置、请求或响应处理失败。"""


class Provider(Protocol):
    # 任何实现该方法的对象都可以被视为 Provider
    """Provider 定义 AgentRuntime 依赖的统一模型调用契约。"""

    def generate(self, messages: list[dict]) -> ProviderResponse:
        """根据标准消息历史生成统一格式的 provider 响应。"""


class StreamingProvider(Protocol):
    """异步流式 Provider 契约；最终必须产出一个 completed 事件。"""

    def stream(self, messages: list[dict]) -> AsyncIterator[ProviderEvent]:
        """按生成顺序产出增量事件和唯一的 completed 事件。"""


class FakeProvider:
    """FakeProvider 是本地 demo / test 使用的确定性假模型。"""

    def generate(self, messages: list[dict]) -> ProviderResponse:
        # 找到最后一条 user 消息，并稳定地 echo 回去，方便测试做精确断言。
        last_user_message = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user_message = message.get("content", "")
                break

        return ProviderResponse(
            text=f"[fake-agent-response] I received: {last_user_message}",
            stop_reason="end_turn",
        )

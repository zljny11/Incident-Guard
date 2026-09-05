from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Sequence

from incident_guard.agents.provider import (
    ProviderError,
    ProviderEvent,
    ProviderEventType,
    ToolCall,
)

ScriptedItem = ProviderEvent | Exception


class ScriptedStreamingProvider:
    """按预设脚本产出事件的确定性异步 Provider。

    每次 ``stream`` 调用消费一段脚本。脚本可以在事件之间放置异常来模拟
    provider 故障；脚本在 completed 事件前耗尽则模拟网络断流。
    """

    def __init__(self, scripts: Iterable[Iterable[ScriptedItem]]) -> None:
        self._scripts = tuple(tuple(script) for script in scripts)
        self._next_script = 0
        self.requests: list[tuple[dict, ...]] = []

    async def stream(self, messages: list[dict]) -> AsyncIterator[ProviderEvent]:
        if self._next_script >= len(self._scripts):
            raise ProviderError("Scripted provider has no remaining stream")

        script = self._scripts[self._next_script]
        self._next_script += 1
        self.requests.append(tuple(dict(message) for message in messages))

        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []

        for index, item in enumerate(script):
            if isinstance(item, Exception):
                if isinstance(item, ProviderError):
                    raise item
                raise ProviderError(f"Scripted provider error: {item}") from item
            if not isinstance(item, ProviderEvent):
                raise ProviderError(
                    "Scripted provider items must be ProviderEvent or Exception"
                )

            if item.event_type is ProviderEventType.TEXT_DELTA:
                assert item.text is not None
                text_chunks.append(item.text)
            elif item.event_type is ProviderEventType.TOOL_CALL:
                assert item.call is not None
                tool_calls.append(item.call)
            else:
                if index != len(script) - 1:
                    raise ProviderError(
                        "Scripted provider completed event must be the final item"
                    )
                assert item.response is not None
                self._validate_completed_event(
                    text_chunks=text_chunks,
                    tool_calls=tool_calls,
                    event=item,
                )

            yield item

            if item.event_type is ProviderEventType.COMPLETED:
                return

        raise ProviderError(
            "Scripted provider stream interrupted before completed event"
        )

    @staticmethod
    def _validate_completed_event(
        *,
        text_chunks: Sequence[str],
        tool_calls: Sequence[ToolCall],
        event: ProviderEvent,
    ) -> None:
        response = event.response
        assert response is not None

        if response.text != "".join(text_chunks):
            raise ProviderError(
                "Scripted provider completed text does not match text deltas"
            )
        if response.tool_calls != tuple(tool_calls):
            raise ProviderError(
                "Scripted provider completed tool calls do not match tool events"
            )

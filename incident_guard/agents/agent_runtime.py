from __future__ import annotations

from incident_guard.agents.provider import Provider


class AgentRuntime:
    """AgentRuntime 负责调用 provider 生成 assistant 回复。"""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def run(self, history: list[dict]) -> str:
        """根据 replay 出来的 session 历史生成回复文本。"""

        response = self.provider.generate(history)
        if response.stop_reason != "end_turn":
            raise ValueError(
                f"Unexpected provider stop_reason: {response.stop_reason}"
            )
        return response.text

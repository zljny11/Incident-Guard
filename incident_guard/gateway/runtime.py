from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from incident_guard.agents.agent_runtime import AgentRuntime
from incident_guard.agents.provider import ProviderError
from incident_guard.agents.provider_factory import create_provider
from incident_guard.gateway.inbound_message import InboundMessage
from incident_guard.observability.trace_logger import TraceLogger
from incident_guard.sessions.session_store import SessionStore


INCIDENT_AGENT_ID = "incident-agent"


@dataclass(slots=True)
class GatewayResult:
    """GatewayRuntime 处理完一条消息后，返回给 demo / API 的结果。"""

    message_id: str
    agent_id: str
    reason: str
    session_id: str
    response_text: str
    trace_path: Path


class GatewayRuntime:
    #AgentRuntime 像“模型发动机”：给它对话历史，它返回模型回复。
    #GatewayRuntime 像“总调度员”：管理消息、会话、日志和异常，并在适当的时候启动模型发动机
    """GatewayRuntime 是 Incident Guard 的单一 Agent 入口。"""

    def __init__(
        self,
        session_store: SessionStore | None = None,
        agent_runtime: AgentRuntime | None = None,
        trace_logger: TraceLogger | None = None,
    ) -> None:
        # Incident Guard 当前只有一个明确的 Agent Profile。
        # 身份信息仍保留在 InboundMessage 中，用于 Session 隔离和审计，
        # 不再通过通用多 Agent 路由表选择历史遗留的业务 Agent。
        self.agent_id = INCIDENT_AGENT_ID
        self.session_store = session_store or SessionStore()
        self.agent_runtime = agent_runtime or AgentRuntime(provider=create_provider())
        self.trace_logger = trace_logger or TraceLogger()

    def handle_message(self, inbound_message: InboundMessage) -> GatewayResult:
        """收到消息，记日志，找到会话，保存输入，读取历史，调用 Agent，保存回复，再把结果交回调用方。"""

        # 1. 记录收到消息
        self.trace_logger.log(
            "message_received",
            metadata={
                "message_id": inbound_message.message_id,
                "channel": inbound_message.channel,
                "account_id": inbound_message.account_id,
                "peer_id": inbound_message.peer_id,
            },
        )

        # 2. 所有输入进入同一个 Incident Agent Profile。
        session_id = (
            f"{inbound_message.account_id}__{inbound_message.channel}__"
            f"{inbound_message.peer_id}__{self.agent_id}"
        )
        reason = "selected single incident-agent profile"
        self.trace_logger.log(
            "agent_selected",
            session_id=session_id,
            metadata={
                "agent_id": self.agent_id,
                "reason": reason,
            },
        )

        # 3. 把用户消息写入 session
        self.session_store.append_message(
            session_id,
            role="user",
            content=inbound_message.text,
            metadata={"message_id": inbound_message.message_id},
        )

        # 4. replay 当前 session 历史，让 agent 能看到完整上下文
        history = self.session_store.replay(session_id)
        self.trace_logger.log(
            "session_replayed",
            session_id=session_id,
            metadata={"message_count": len(history)},
        )

        # 5. 调 agent 生成回复
        try:
            response_text = self.agent_runtime.run(history)
        except ProviderError as error:
            self.trace_logger.log(
                "provider_error",
                status="error",
                session_id=session_id,
                error=str(error),
            )
            raise

        # 6. 把 assistant 回复写入 session，并返回 GatewayResult
        self.session_store.append_message(
            session_id,
            role="assistant",
            content=response_text,
        )
        self.trace_logger.log(
            "agent_response_generated",
            session_id=session_id,
            metadata={"response_text": response_text},
        )

        return GatewayResult(
            message_id=inbound_message.message_id,
            agent_id=self.agent_id,
            reason=reason,
            session_id=session_id,
            response_text=response_text,
            trace_path=self.trace_logger.trace_path,
        )

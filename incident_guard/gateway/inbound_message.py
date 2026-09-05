from __future__ import annotations

from dataclasses import dataclass, field
from time import time
from uuid import uuid4


@dataclass(slots=True) #对象只允许使用预先定义的字段
class InboundMessage:
    """InboundMessage 是进入 GatewayRuntime 前的标准消息格式。"""

    message_id: str
    channel: str
    account_id: str
    peer_id: str
    text: str
    timestamp: float
    metadata: dict = field(default_factory=dict)

    @staticmethod
    def create(
        channel: str,
        account_id: str,
        peer_id: str,
        text: str,
        metadata: dict | None = None,
    ) -> "InboundMessage":
        """创建一条带 message_id 和 timestamp 的标准消息。"""

        return InboundMessage(
            message_id=str(uuid4()),
            channel=channel,
            account_id=account_id,
            peer_id=peer_id,
            text=text,
            timestamp=time(),
            metadata=metadata or {},
        )

from __future__ import annotations

from incident_guard.channels.base import require_text_field
from incident_guard.gateway.inbound_message import InboundMessage


class CliChannelAdapter:
    """CliChannelAdapter 负责把 CLI 输入转换成 InboundMessage。"""

    def to_inbound_message(self, raw_event: dict) -> InboundMessage:
        """处理 CLI raw event，并返回 GatewayRuntime 的标准输入。"""

        # CLI raw event 约定：
        # account_id 表示当前账号
        # peer_id 表示当前 CLI 用户或操作者
        # text 表示用户输入内容
        account_id = require_text_field(raw_event, "account_id")
        peer_id = require_text_field(raw_event, "peer_id")
        text = require_text_field(raw_event, "text")

        # adapter 输出统一的 InboundMessage，后面的 runtime 不再关心 raw event。
        return InboundMessage.create(
            channel="cli",
            account_id=account_id,
            peer_id=peer_id,
            text=text,
            metadata={
                "source": "cli",
                **raw_event.get("metadata", {}),
            },
        )

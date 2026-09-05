from __future__ import annotations

from incident_guard.channels.base import require_text_field
from incident_guard.gateway.inbound_message import InboundMessage


class MockWebhookAdapter:
    """MockWebhookAdapter 负责把模拟 Webhook 事件转换成 InboundMessage。"""

    def to_inbound_message(self, raw_event: dict) -> InboundMessage:
        """处理 mock webhook raw event，并返回 GatewayRuntime 的标准输入。"""

        # mock web raw event 故意使用和 CLI 不同的字段名：
        # account 表示当前账号
        # user 表示 Web 端用户
        # message 表示用户输入内容
        account_id = require_text_field(raw_event, "account")
        peer_id = require_text_field(raw_event, "user")
        text = require_text_field(raw_event, "message")

        # request_id 是 Web 请求侧的信息，保留到 metadata，方便后续排查。
        metadata = {
            "source": "mock-web",
        }
        if "request_id" in raw_event:
            metadata["request_id"] = raw_event["request_id"]

        return InboundMessage.create(
            channel="web",
            account_id=account_id,
            peer_id=peer_id,
            text=text,
            metadata=metadata,
        )

from __future__ import annotations

from typing import Protocol

from incident_guard.gateway.inbound_message import InboundMessage


class ChannelAdapter(Protocol):
    """ChannelAdapter 负责把不同来源的 raw event 转成统一的 InboundMessage。"""

    def to_inbound_message(self, raw_event: dict) -> InboundMessage:
        """把某个渠道自己的原始消息格式，转换成 GatewayRuntime 能处理的格式。"""


def require_text_field(raw_event: dict, field_name: str) -> str:
    """读取必填字段；缺失或为空时抛出清晰错误。"""

    value = raw_event.get(field_name)
    if value is None or value == "":
        raise ValueError(f"raw_event missing required field: {field_name}")
    return str(value)

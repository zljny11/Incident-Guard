from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import time


@dataclass(slots=True)
class SessionSummary:
    """会话概览本"""

    session_id: str
    message_count: int
    last_updated: float | None


class SessionStore:
    """SessionStore 负责保存 / 读取对话历史。"""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path("data/sessions")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
        """把一条 user / assistant 消息追加写入 session 文件。"""

        record = {
            "role": role,
            "content": content,
            "timestamp": time(),
            "metadata": metadata or {},
        }
        session_path = self.base_dir / f"{session_id}.jsonl"
        with session_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def replay(self, session_id: str, limit: int | None = None) -> list[dict]:
        """按写入顺序读取当前 session 的历史消息。"""

        session_path = self.base_dir / f"{session_id}.jsonl"
        if not session_path.exists():
            return []

        # limit 是 replay window：只限制这次读出来多少条，不修改原始 JSONL 文件。
        with session_path.open("r", encoding="utf-8") as handle:
            messages = [json.loads(line) for line in handle if line.strip()]

        if limit is None:
            return messages
        if limit <= 0:
            return []
        return messages[-limit:]

    def list_sessions(self) -> list[SessionSummary]:
        """列出当前 base_dir 里的所有 session 概览。"""

        summaries = []
        for session_path in sorted(self.base_dir.glob("*.jsonl")):
            # metadata 从 JSONL 计算出来，避免 MVP 阶段维护额外索引文件。
            messages = self._read_session_file(session_path)
            last_updated = None
            if messages:
                last_updated = messages[-1].get("timestamp")
            summaries.append(
                SessionSummary(
                    session_id=session_path.stem,
                    message_count=len(messages),
                    last_updated=last_updated,
                )
            )
        return summaries

    def _read_session_file(self, session_path: Path) -> list[dict]:
        """读取某个 session 文件的全部消息。"""

        with session_path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

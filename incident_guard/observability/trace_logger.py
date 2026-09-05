from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from uuid import uuid4


@dataclass(slots=True)
class TraceLogger:
    """TraceLogger 负责记录一次 gateway 执行过程中发生了什么。
    session只会告诉最终状态 但粒度不够 TraceLogger 可以记录每个步骤的状态 方便排查问题"""

    base_dir: Path = field(default_factory=lambda: Path("data/traces"))
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    trace_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.base_dir / f"{self.trace_id}.jsonl"

    def log(
        self,
        event_type: str,
        status: str = "success",
        session_id: str | None = None,
        metadata: dict | None = None,
        error: str | None = None,
    ) -> None:
        """往 trace JSONL 文件里追加一条结构化事件。"""

        record = {
            "trace_id": self.trace_id,
            "event_type": event_type,
            "status": status,
            "session_id": session_id,
            "timestamp": time(),
            "metadata": metadata or {},
            "error": error,
        }
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

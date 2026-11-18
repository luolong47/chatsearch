"""统一数据模型：两种数据源解析后都归一为 Session / Message。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["user", "assistant"]
Source = Literal["workbuddy", "dsh"]


@dataclass
class Message:
    role: Role
    text: str
    ts: int = 0  # 毫秒时间戳
    seq: int = 0  # 会话内序号，保证时间线顺序

    def is_indexable(self) -> bool:
        return bool(self.text and self.text.strip())


@dataclass
class Session:
    source: Source
    sid: str  # 来源内唯一会话 id
    title: str
    messages: list[Message] = field(default_factory=list)
    cwd: str = ""
    archived: bool = False  # WorkBuddy: sessions.status=='archived'；DSH: workspace.json 的 archivedSessionIds
    created_at: int = 0  # 毫秒
    updated_at: int = 0  # 毫秒
    native_status: str = ""  # 原生状态：WorkBuddy 的 sessions.status（active/working/completed/archived）
    last_active: int = 0  # 原生最近活动时间（毫秒），WorkBuddy 的 last_activity_at
    # 会话文件最后一条有效事件是否已经收尾（assistant 的最终回复）。
    # True=这一轮已结束；False=写到一半（生成中 / 被杀）；None=不知道（DSH 无此结构）。
    tail_closed: bool | None = None

    @property
    def key(self) -> str:
        return f"{self.source}:{self.sid}"

    def sorted_messages(self) -> list[Message]:
        return sorted(self.messages, key=lambda m: (m.ts, m.seq))

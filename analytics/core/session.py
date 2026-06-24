"""会话管理 — 内存存储多轮对话历史."""

from __future__ import annotations

import time
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# 单会话最大消息数（防止无限增长）
_MAX_MESSAGES = 40
# 最大会话数
_MAX_SESSIONS = 1000


@dataclass
class Session:
    """单个会话."""

    session_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def add_message(self, msg: dict[str, Any]) -> None:
        """追加消息，超出上限时裁剪旧消息（保留 system prompt）."""
        self.messages.append(msg)
        self.updated_at = time.time()
        # 裁剪：保留最近的 _MAX_MESSAGES 条
        if len(self.messages) > _MAX_MESSAGES:
            self.messages = self.messages[-_MAX_MESSAGES:]

    def get_messages(self) -> list[dict[str, Any]]:
        """获取完整历史."""
        return self.messages

    def clear(self) -> None:
        """清空历史."""
        self.messages.clear()
        self.updated_at = time.time()


class SessionStore:
    """内存会话存储."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str) -> Session:
        """获取或创建会话."""
        if session_id not in self._sessions:
            # 简单淘汰：超出上限时删除最旧的
            if len(self._sessions) >= _MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda k: self._sessions[k].updated_at)
                del self._sessions[oldest]
                log.info(f"Session store full, evicted oldest: {oldest}")
            self._sessions[session_id] = Session(session_id=session_id)
        return self._sessions[session_id]

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def clear_all(self) -> None:
        self._sessions.clear()


# 全局单例
_store = SessionStore()


def get_session(session_id: str) -> Session:
    return _store.get_or_create(session_id)


def remove_session(session_id: str) -> None:
    _store.remove(session_id)

"""AgentMemory 接口 — 对话记忆抽象.

Agent 通过此接口保存/加载/搜索历史对话，用于上下文复用。
当前实现: 文件系统 JSONL (forecast 已有)，未来可换向量数据库。

用法::

    from infra.agent.memory import AgentMemory

    class JSONLMemory(AgentMemory):
        async def save(self, session_id, messages): ...
        async def load(self, session_id) -> list: ...
        async def search(self, query, limit) -> list: ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class AgentMemory(ABC):
    """Agent 记忆接口.

    子类实现:
        - JSONLMemory — 本地文件 (当前)
        - RedisMemory — Redis (多 worker)
        - VectorMemory — 向量检索 (语义搜索)
    """

    @abstractmethod
    async def save(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """保存会话消息."""

    @abstractmethod
    async def load(self, session_id: str) -> list[dict[str, Any]]:
        """加载会话消息."""

    @abstractmethod
    async def search(self, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        """搜索相关历史消息."""

    @abstractmethod
    async def delete(self, session_id: str) -> None:
        """删除会话."""

"""Assistant Bot — 计算结果解读的 Pydantic 数据模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# AssistantMessage — 复用 ChatMessage 结构，单独定义避免耦合
# ---------------------------------------------------------------------------

class AssistantMessage(BaseModel):
    """Assistant 对话中的单条消息。"""
    role: str = "user"                     # user / assistant / system
    content: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class AssistantSessionCreate(BaseModel):
    """创建 Assistant 会话的请求体。"""
    skill_id: str
    data: dict[str, Any] | list[Any]       # 前端选中的数据行 + 预测结果
    title: str = "结果解读"


class AssistantSession(BaseModel):
    """完整的 Assistant 会话。"""
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    title: str = "结果解读"
    skill_id: str
    data: dict[str, Any] | list[Any]
    messages: list[AssistantMessage] = Field(default_factory=list)
    system_prompt: str = ""                # 内部使用，不返回前端
    created_at: float = Field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    updated_at: float = Field(default_factory=lambda: datetime.now(timezone.utc).timestamp())


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

class AssistantMessageRequest(BaseModel):
    """用户发送的消息请求。"""
    message: str = Field(..., max_length=5000)

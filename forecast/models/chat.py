"""聊天会话与消息的 Pydantic 数据模型。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# ChatMessage
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    """聊天对话中的单条消息。"""
    role: str = "user"                              # user / assistant / system / tool
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None  # assistant 工具调用块
    tool_call_id: str | None = None                  # 工具结果关联ID
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# ChatSession
# ---------------------------------------------------------------------------

class ChatSessionCreate(BaseModel):
    """创建新聊天会话的请求体。"""
    title: str = "New Forecast Chat"
    input_data: dict[str, Any] | list[Any] | None = None        # forecast.json 数据（单条或数组）


class ChatSession(BaseModel):
    """完整的聊天会话，包含所有消息。"""
    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    title: str = "New Forecast Chat"
    messages: list[ChatMessage] = Field(default_factory=list)
    input_data: dict[str, Any] | list[Any] | None = None
    target_skill_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Chat request / response
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    """用户发送的聊天消息。"""
    message: str = Field(..., max_length=10000)
    attach_data: dict[str, Any] | list[Any] | None = None       # 可选的附加数据（单条或数组）


class ChatResponse(BaseModel):
    """非流式聊天响应（用于确认/预览接口）。"""
    session_id: str
    message: ChatMessage

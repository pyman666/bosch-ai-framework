"""请求模型."""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """对话请求."""

    message: str = Field(..., description="用户自然语言问题", example="看一下上个月各渠道的订单量")
    session_id: str | None = Field(default=None, description="会话ID，用于多轮对话")
    context: dict | None = Field(default=None, description="额外上下文（如当前筛选条件）")

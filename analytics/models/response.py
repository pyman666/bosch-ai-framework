"""响应模型."""

from pydantic import BaseModel, Field


class ChartConfig(BaseModel):
    """图表配置."""

    type: str = Field(..., description="图表类型", example="bar")
    option: dict = Field(..., description="ECharts option JSON")


class ChatResponse(BaseModel):
    """对话响应."""

    reply: str = Field(..., description="文字回复")
    chart: ChartConfig | None = Field(default=None, description="图表配置（前端直接 setOption）")
    data: list[dict] | None = Field(default=None, description="原始数据")
    sources: list[str] = Field(default_factory=list, description="数据来源 BFF 列表")
    session_id: str = Field(..., description="会话ID")
    insights: list[str] | None = Field(default=None, description="数据洞察（异常解释等）")

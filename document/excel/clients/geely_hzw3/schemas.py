"""geely-hzw3 客户的 Row + Data schema.

数据形态 = "缺口信息" 时序表: 每行一个零件 (partNo + 现状库存) + 接下来若干天的
缺口数 (data: list of {date, qty}).
"""
from pydantic import BaseModel, ConfigDict, Field


class GeelyHzw3Data(BaseModel):
    """unpivot 后的一条时序记录."""
    date: str = Field(..., description="日期 (ISO `YYYY-MM-DD` 或原 cell ISO 字符串)")
    qty: int | float = Field(..., description="缺口数, 负数表示缺料")


class GeelyHzw3Row(BaseModel):
    """每个零件一行: id 字段平铺 + ``data`` 装时序."""
    model_config = ConfigDict(extra="allow")

    partNo: str = Field(..., description="零件号")
    data: list[GeelyHzw3Data] = Field(..., description="按日期排列的缺口序列")



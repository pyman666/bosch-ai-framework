"""geely-ms 客户的 Row + Data schema.

数据形态 = "需求合计" 跟踪表 (sheet "跟踪表"): 每行一个零件 (基础属性 + 当前 MIN/MAX
+ 供应商 + 分工人员) + 接下来约 47 天的需求合计 (data: list of {date, qty}).
"""
from pydantic import BaseModel, ConfigDict, Field


class GeelyMsData(BaseModel):
    """unpivot 后的一条需求合计记录."""
    date: str = Field(..., description="日期 (ISO `YYYY-MM-DD`)")
    qty: int | float = Field(..., description="该日需求合计")


class GeelyMsRow(BaseModel):
    """每个零件一行: id 字段平铺 + ``data`` 装时序."""
    model_config = ConfigDict(extra="allow")

    partNo: str = Field(..., description="物料号")
    partDesc: str | None = Field(None, description="物料描述")
    workshop: str | None = Field(None, description="使用车间")
    supplierCode: str | None = Field(None, description="供应商编码")
    data: list[GeelyMsData] = Field(..., description="按日期排列的需求合计序列")


__all__ = ["GeelyMsData", "GeelyMsRow"]

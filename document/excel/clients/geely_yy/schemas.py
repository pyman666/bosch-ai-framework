"""geely-yy 客户的 Row + Data schema.

数据形态 = 多车型缺口报表: 每行一个零件 (partNo + 供应商 + 仓库 + 库存合计 + 在途) +
接下来若干 (车型, 日期) 二维组合的需求数 (data: list of {var, qty}, var = "<车型>/<日期>").

跟 hzw3/ms 的区别: yy 的动态列是**二级 group**, 既有车型 (R1) 又有日期 (R3).
通用层 ``WideExcelConfig.var_header_rows=[1, 3]`` 自动把两层拼成 ``"CX11 A3 L/2025-12-30"``
塞进每条 ``data`` 记录的 ``var`` 字段, 业务方拿到后想拆按 ``"/"`` split 即可.
"""
from pydantic import BaseModel, ConfigDict, Field


class GeelyYyData(BaseModel):
    """unpivot 后的一条 (车型, 日期, 需求数) 记录."""
    date: str = Field(..., description="日期组合")
    qty: int | float = Field(..., description="需求数")


class GeelyYyRow(BaseModel):
    """每个零件一行: id 字段平铺 + ``data`` 装 (车型, 日期) 时序."""
    model_config = ConfigDict(extra="allow")

    partNo: str = Field(..., description="物料编码")
    supplierCode: str | None = Field(None, description="供应商代码 1 (其它供应商代码列在原表里隐藏, 不输出)")
    warehouse: str | None = Field(None, description="仓库")
    carModel: str | None = Field(None, description="车型")
    data: list[GeelyYyData] = Field(..., description="按 (车型, 日期) 拼合的需求序列")


__all__ = ["GeelyYyData", "GeelyYyRow"]

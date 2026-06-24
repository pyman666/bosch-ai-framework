"""geely-cd 客户的 Row + Data schema.

数据形态 = "总装上线计划汇总" sheet 顶部的**车型汇总**段: 每行一个 (车型, 内饰颜色)
组合 (e.g. 'A3油车汇总' / 'A3油车汇总 + 哑光卡其绿' / 'P22H-E新款410'), 后面 31 个
ISO 日期列 (当月每天的整车上线计划数). data: list of {date, qty}.

下方按 '配置' 拆分的逐细分行 (品牌=领克国内 / 车型=BX11 A3 等) **不抽**, 由业务方
另用 detail-port 处理 (本 client 只关心车型汇总段).
"""
from pydantic import BaseModel, ConfigDict, Field


class GeelyCdData(BaseModel):
    """unpivot 后的一条上线计划记录."""
    date: str = Field(..., description="日期 (ISO `YYYY-MM-DD`)")
    qty: int | float = Field(..., description="该日上线计划数 (整数为主, 0 即当日不排产)")


class GeelyCdRow(BaseModel):
    """每个 (车型, 内饰颜色) 一行: id 字段平铺 + ``data`` 装时序."""
    model_config = ConfigDict(extra="allow")

    carModel: str = Field(
        ...,
        description="车型 / 配置名 (e.g. 'A3油车汇总' / 'P22H-E新款410' / 'E335（欧洲）')",
    )
    color: str | None = Field(
        None,
        description=(
            "内饰颜色子分类 (合并 cell 段, 例: 'A3油车汇总' 下的 '其他颜色' / '哑光卡其绿'). "
            "无子分类时 = 父行 config 文本本身 (matrix_filled 把合并 cell 值填到 col4)."
        ),
    )
    data: list[GeelyCdData] = Field(..., description="按日期排列的上线计划序列")



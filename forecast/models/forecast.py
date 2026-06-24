"""预测输入/输出的 Pydantic 数据模型 — 对齐 docs/forecast.json 格式。"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Time-series point
# ---------------------------------------------------------------------------

class TimeSeriesPoint(BaseModel):
    """单个 (日期, 数量) 数据点。"""
    date: date
    qty: float


# ---------------------------------------------------------------------------
# Forecast input — maps to each item in forecast.json array
# ---------------------------------------------------------------------------

class ForecastInput(BaseModel):
    """单条预测输入记录。

    需求/库存/业务参数用于计算；未声明的标识字段（如 carModel、color、
    partNo、supplier 等）由 ``extra="allow"`` 自动捕获并透传到输出。
    """
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    demand: list[TimeSeriesPoint] = Field(default_factory=list)
    pgi: list[TimeSeriesPoint] = Field(default_factory=list)
    beginning_inventory: float = Field(default=0.0, alias="beginningInventory")
    other_factors: Any = Field(
        default_factory=dict,
        validation_alias=AliasChoices("other_factors", "other_factors_to_be_added"),
        serialization_alias="other_factors_to_be_added",
    )

    # ---- 业务逻辑扩展字段（可选，用于预设算法） ----

    # 模式 A：周需求 + JITCall
    weekly_demand: float = Field(
        default=0.0,
        description="周需求总量，用于 JITCall 优先级预设（模式 A）",
    )
    jitcall: list[TimeSeriesPoint] = Field(
        default_factory=list,
        description="JITCall 取货订单时间序列，优先级高于日需求",
    )
    transportation_lt: int = Field(
        default=0,
        alias="transportationLT",
        description="运输提前量（天），用于判断已释放的日需求范围",
    )

    # 模式 B：月预测 + 日需求整合
    monthly_forecast: float | dict[str, float] = Field(
        default=0.0,
        description="月预测总量，用于月预测+日需求整合预设（模式 B）。支持 float 或按月字典 {'2026-01': 8000, '2026-02': 9000}",
    )

    # ------------------------------------------------------------------
    # Helpers for downstream output construction
    # ------------------------------------------------------------------

    # ForecastOutput 的声明字段，不应从 model_extra 透传（否则会触发
    # "multiple values for keyword argument" 错误）
    _OUTPUT_RESERVED_KEYS = frozenset({"forecast", "metadata"})

    @property
    def extra_for_output(self) -> dict[str, Any]:
        """返回 model_extra 中与 ForecastOutput 声明字段不冲突的子集。

        用于 ``ForecastOutput(**inp.extra_for_output, ...)`` 透传身份字段时
        避免命名冲突。
        """
        return {k: v for k, v in (self.model_extra or {}).items()
                if k not in self._OUTPUT_RESERVED_KEYS}

    ins: list[TimeSeriesPoint] = Field(
        default_factory=list,
        description="预计到货（INS）时间序列，用于库存 Balance 计算",
    )
    forecast_period: int = Field(
        default=0,
        description="预测周期（天数），控制输出长度",
    )


# ---------------------------------------------------------------------------
# Forecast output
# ---------------------------------------------------------------------------

class ForecastOutput(BaseModel):
    """预测 Skill 执行的输出结果。"""
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    forecast: list[TimeSeriesPoint] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Trial calculation
# ---------------------------------------------------------------------------

class TrialCalculationRequest(BaseModel):
    """使用临时公式进行试算的请求。"""
    dsl_expression: str | None = None
    python_code: str | None = None
    input_data: list[ForecastInput] = Field(default_factory=list)


class TrialCalculationResponse(BaseModel):
    """试算结果。"""
    results: list[ForecastOutput] = Field(default_factory=list)
    error: str | None = None


# ---------------------------------------------------------------------------
# Forecast accuracy evaluation
# ---------------------------------------------------------------------------

class ForecastAccuracyRequest(BaseModel):
    """预测准确度评估请求。"""
    forecast: list[TimeSeriesPoint] = Field(..., description="预测值时间序列")
    actual: list[TimeSeriesPoint] = Field(..., description="实际值时间序列")


class ForecastAccuracyMetrics(BaseModel):
    """预测准确度指标。"""
    mae: float | None = Field(..., description="平均绝对误差 (Mean Absolute Error)")
    mape: float | None = Field(..., description="平均绝对百分比误差 (Mean Absolute Percentage Error)")
    rmse: float | None = Field(..., description="均方根误差 (Root Mean Squared Error)")
    smape: float | None = Field(..., description="对称平均绝对百分比误差 (Symmetric MAPE)")
    data_points: int = Field(..., description="参与计算的数据点数量")


class ForecastAccuracyResponse(BaseModel):
    """预测准确度评估响应。"""
    metrics: ForecastAccuracyMetrics

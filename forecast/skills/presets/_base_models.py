"""基础模型预设 — zero_shot、timesfm、chronos（自动降级策略）。"""

from __future__ import annotations

from typing import Any

from forecast.skills.presets._helpers import demand_series
from forecast.skills.presets._statistical import (
    exponential_smoothing,
    linear_trend,
    moving_average,
)


def zero_shot(record: dict[str, Any], horizon: int = 7) -> list[dict[str, Any]]:
    """Zero-shot 预测 — 使用 Holt-Winters 作为核心引擎，带自动降级。

    降级策略：
    1. 数据 >= 2*seasonal_periods → Holt-Winters 完整模式
    2. 数据 >= 4 → 线性趋势
    3. 数据 < 4 → 简单移动平均
    """
    # 延迟导入 statsmodels 预设，避免无 statsmodels 时启动失败
    from forecast.skills.presets._statsmodels import holt_winters

    demand = demand_series(record)
    if len(demand) >= 14:  # 至少 2 个周期才能用 Holt-Winters
        try:
            return holt_winters(record, horizon=horizon)
        except Exception:
            pass  # fallback to simpler methods
    if len(demand) >= 4:
        return linear_trend(record, window=min(14, len(demand)))[:horizon]
    return moving_average(record, window=max(1, min(horizon, len(demand) or horizon)))[:horizon]


def timesfm(record: dict[str, Any], horizon: int = 7) -> list[dict[str, Any]]:
    """TimesFM preset placeholder; replace with real model/service when available."""
    return linear_trend(record, window=30)[:horizon]


def chronos(record: dict[str, Any], horizon: int = 7) -> list[dict[str, Any]]:
    """Chronos 预测 — 使用 ARIMA 作为核心引擎，带自动降级。

    降级策略：
    1. 数据 >= 10 → ARIMA 自动选参
    2. 数据 >= 4 → 线性趋势
    3. 数据 < 4 → 指数平滑
    """
    # 延迟导入 statsmodels 预设
    from forecast.skills.presets._statsmodels import arima

    demand = demand_series(record)
    if len(demand) >= 10:
        try:
            return arima(record, horizon=horizon)
        except Exception:
            pass  # fallback
    if len(demand) >= 4:
        return linear_trend(record, window=min(14, len(demand)))[:horizon]
    return exponential_smoothing(record, alpha=0.35)[:horizon]

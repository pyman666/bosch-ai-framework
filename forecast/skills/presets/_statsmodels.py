"""基于 statsmodels 的预设 — Holt-Winters、ARIMA。"""

from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any
import numpy as np

from forecast.skills.presets._helpers import demand_series

log = logging.getLogger(__name__)

_ARIMA_TIMEOUT = 10  # 单条 ARIMA 网格搜索最大耗时 (秒)


def holt_winters(
    record: dict[str, Any],
    horizon: int = 7,
    seasonal_periods: int = 7,
) -> list[dict[str, Any]]:
    """Holt-Winters 三次指数平滑预测。

    自动检测趋势和季节性，使用 statsmodels 的 ExponentialSmoothing。
    策略：
    1. 数据 >= 2*seasonal_periods → 带季节性的 Holt-Winters
    2. 数据 >= 4 → Holt 双指数平滑（仅趋势）
    3. 数据 < 4 → 简单移动平均 fallback
    """
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    # 延迟导入以避免循环依赖
    from forecast.skills.presets._statistical import linear_trend, moving_average

    demand = demand_series(record)
    n = len(demand)
    if n == 0:
        return []

    data = np.array(demand, dtype=float)
    last_date = record["demand"][-1]["date"] if record.get("demand") else dt.date.today()
    if isinstance(last_date, str):
        last_date = dt.date.fromisoformat(last_date)

    def _make_result(forecast_values):
        return [
            {"date": (last_date + dt.timedelta(days=i + 1)).isoformat(),
             "qty": round(float(max(0, float(v))), 2)}
            for i, v in enumerate(forecast_values[:horizon])
        ]

    if n < 4:
        return moving_average(record, window=max(1, min(horizon, n)))[:horizon]

    if n >= 2 * seasonal_periods:
        try:
            safe_data = np.maximum(data, 0.01)
            model = ExponentialSmoothing(
                safe_data,
                seasonal_periods=seasonal_periods,
                trend="add",
                seasonal="add",
                initialization_method="estimated",
            )
            fit = model.fit(optimized=True, use_brute=True)
            forecast = fit.forecast(horizon)
            return _make_result(forecast)
        except Exception as e:
            log.warning("Holt-Winters seasonal fit failed, falling back: %s", e)

    try:
        safe_data = np.maximum(data, 0.01)
        model = ExponentialSmoothing(
            safe_data,
            trend="add",
            initialization_method="estimated",
        )
        fit = model.fit(optimized=True)
        forecast = fit.forecast(horizon)
        return _make_result(forecast)
    except Exception as e:
        log.warning("Holt-Winters additive trend fit failed, falling back: %s", e)

    return linear_trend(record, window=min(14, n))[:horizon]


def _arima_fit(
    safe_data: np.ndarray,
    actual_max_p: int,
    actual_max_d: int,
    actual_max_q: int,
) -> tuple[Any, tuple[int, int, int]]:
    """ARIMA 网格搜索（在独立线程中执行，外覆超时保护）。"""
    best_aic = float("inf")
    best_order = (1, 1, 1)
    best_model = None

    from statsmodels.tsa.arima.model import ARIMA as ARIMA_MODEL

    for p in range(actual_max_p + 1):
        for d in range(actual_max_d + 1):
            for q in range(actual_max_q + 1):
                if p == 0 and q == 0:
                    continue
                try:
                    model = ARIMA_MODEL(safe_data, order=(p, d, q))
                    fit = model.fit()
                    if fit.aic < best_aic:
                        best_aic = fit.aic
                        best_order = (p, d, q)
                        best_model = fit
                except Exception:
                    continue

    return best_model, best_order


def arima(
    record: dict[str, Any],
    horizon: int = 7,
    max_p: int = 3,
    max_d: int = 2,
    max_q: int = 3,
) -> list[dict[str, Any]]:
    """ARIMA 自动选参预测。

    基于 AIC 准则自动选择最优 (p, d, q) 参数组合。
    使用 statsmodels 的 ARIMA 实现。
    单条网格搜索有 10s 超时保护，超时自动 fallback 到 linear_trend。
    """

    # 延迟导入以避免循环依赖
    from forecast.skills.presets._statistical import exponential_smoothing, linear_trend

    demand = demand_series(record)
    n = len(demand)
    if n == 0:
        return []

    data = np.array(demand, dtype=float)
    last_date = record["demand"][-1]["date"] if record.get("demand") else dt.date.today()
    if isinstance(last_date, str):
        last_date = dt.date.fromisoformat(last_date)

    def _make_result(forecast_values):
        return [
            {"date": (last_date + dt.timedelta(days=i + 1)).isoformat(),
             "qty": round(float(max(0, float(v))), 2)}
            for i, v in enumerate(forecast_values[:horizon])
        ]

    if n < 4:
        return exponential_smoothing(record, alpha=0.3)[:horizon]

    safe_data = np.maximum(data, 0.01)

    actual_max_p = min(max_p, max(1, n // 4))
    actual_max_d = min(max_d, 1)
    actual_max_q = min(max_q, max(1, n // 4))

    best_model = None

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _arima_fit, safe_data, actual_max_p, actual_max_d, actual_max_q,
        )
        try:
            best_model, best_order = future.result(timeout=_ARIMA_TIMEOUT)
        except FuturesTimeoutError:
            future.cancel()
            log.warning("ARIMA grid search timed out (%ds), falling back to linear_trend", _ARIMA_TIMEOUT)
        except Exception as e:
            log.warning("ARIMA grid search failed: %s, falling back", e)

    if best_model is not None:
        try:
            forecast = best_model.forecast(steps=horizon)
            return _make_result(forecast)
        except Exception as e:
            log.warning("ARIMA forecast failed, falling back: %s", e)

    return linear_trend(record, window=min(14, n))[:horizon]

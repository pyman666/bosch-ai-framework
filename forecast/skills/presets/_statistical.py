"""统计类预设 — 移动平均、指数平滑、线性趋势、安全库存、库存优化。"""

from __future__ import annotations

import datetime as dt
from typing import Any

from forecast.skills.presets._helpers import demand_series, pgi_series


def moving_average(record: dict[str, Any], window: int = 7) -> list[dict[str, Any]]:
    demand = demand_series(record)
    if not demand:
        return []
    last = demand[-min(window, len(demand)):]
    avg = sum(last) / len(last)
    last_date = record["demand"][-1]["date"] if record.get("demand") else dt.date.today()
    if isinstance(last_date, str):
        last_date = dt.date.fromisoformat(last_date)
    return [
        {"date": (last_date + dt.timedelta(days=i + 1)).isoformat(), "qty": round(float(avg), 2)}
        for i in range(window)
    ]


def exponential_smoothing(record: dict[str, Any], alpha: float = 0.3) -> list[dict[str, Any]]:
    demand = demand_series(record)
    if not demand:
        return []
    smoothed = demand[0]
    for q in demand[1:]:
        smoothed = alpha * q + (1 - alpha) * smoothed
    last_date = record["demand"][-1]["date"] if record.get("demand") else dt.date.today()
    if isinstance(last_date, str):
        last_date = dt.date.fromisoformat(last_date)
    return [
        {"date": (last_date + dt.timedelta(days=i + 1)).isoformat(), "qty": round(float(smoothed), 2)}
        for i in range(7)
    ]


def linear_trend(record: dict[str, Any], window: int = 30) -> list[dict[str, Any]]:
    demand = demand_series(record)
    if not demand:
        return []
    n = min(window, len(demand))
    recent = demand[-n:]
    x_mean = (n - 1) / 2.0
    y_mean = sum(recent) / n
    num = sum((i - x_mean) * (recent[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den != 0 else 0
    intercept = y_mean - slope * x_mean

    last_date = record["demand"][-1]["date"] if record.get("demand") else dt.date.today()
    if isinstance(last_date, str):
        last_date = dt.date.fromisoformat(last_date)
    result = []
    horizon = min(n, 30)
    for i in range(horizon):
        q = intercept + slope * (n + i)
        result.append({
            "date": (last_date + dt.timedelta(days=i + 1)).isoformat(),
            "qty": round(float(max(0, q)), 2),
        })
    return result


def safety_stock_planning(record: dict[str, Any], z_score: float = 1.65, window: int = 30) -> list[dict[str, Any]]:
    """安全库存计划：需求预测 + 安全库存 − 期初库存 − PGI。"""
    demand = demand_series(record)
    pgi = pgi_series(record)
    begin_inv = float(record.get("beginningInventory", 0))

    if not demand:
        return []

    n = min(window, len(demand))
    recent = demand[-n:]
    avg_demand = sum(recent) / n
    std_demand = (sum((q - avg_demand) ** 2 for q in recent) / n) ** 0.5

    safety_stock = z_score * std_demand

    last_date = record["demand"][-1]["date"] if record.get("demand") else dt.date.today()
    if isinstance(last_date, str):
        last_date = dt.date.fromisoformat(last_date)

    result = []
    inv = begin_inv
    for i in range(min(n, 30)):
        d = avg_demand
        p = pgi[i] if i < len(pgi) else 0
        shipment = max(0, d + safety_stock - inv - p)
        inv = max(0, inv + p - d)
        result.append({
            "date": (last_date + dt.timedelta(days=i + 1)).isoformat(),
            "qty": round(float(shipment), 2),
            "inventory": round(float(inv), 2),
        })
    return result


def inventory_optimization(record: dict[str, Any], service_level: float = 0.95) -> list[dict[str, Any]]:
    """综合库存优化：用服务水准查 z_score，委托给 safety_stock_planning。"""
    from scipy.stats import norm as scipy_norm

    z_score = scipy_norm.ppf(service_level)
    return safety_stock_planning(record, z_score=z_score)

"""预设工具函数 — 需求序列提取、日期解析等。"""

from __future__ import annotations

import datetime as dt
from typing import Any


def parse_demand_series(raw: list[Any]) -> list[dict[str, Any]]:
    """Parse raw demand/jitcall/pgi entries into [{date, qty}] with date objects."""
    entries = []
    for item in raw:
        d = dt.date.fromisoformat(item["date"]) if isinstance(item["date"], str) else item["date"]
        q = float(item.get("qty", 0)) if isinstance(item, dict) else float(item)
        entries.append({"date": d, "qty": q})
    return entries


def build_date_qty_map(raw: list[Any]) -> dict[dt.date, float]:
    """Build date -> qty sum map from raw entries."""
    m: dict[dt.date, float] = {}
    for item in raw:
        d = dt.date.fromisoformat(item["date"]) if isinstance(item["date"], str) else item["date"]
        q = float(item.get("qty", 0)) if isinstance(item, dict) else float(item)
        m[d] = m.get(d, 0) + q
    return m


def demand_series(record: dict[str, Any]) -> list[float]:
    """从预测记录中提取需求数量序列。"""
    demand = record.get("demand", [])
    if not demand:
        return []
    return [d["qty"] if isinstance(d, dict) else float(d) for d in demand]


def pgi_series(record: dict[str, Any]) -> list[float]:
    """从预测记录中提取 PGI 在途数量序列。"""
    pgi = record.get("pgi", [])
    if not pgi:
        return []
    return [p["qty"] if isinstance(p, dict) else float(p) for p in pgi]


def prep_demand(record: dict[str, Any]) -> list[dict[str, Any]]:
    """从 record 提取 demand 字段，解析为排序后的 [{date, qty}]。无有效数据返回 []。"""
    raw = record.get("demand", [])
    if not raw:
        return []
    days = parse_demand_series(raw)
    if not days:
        return []
    days.sort(key=lambda x: x["date"])
    return days


def merge_sum_by_key(
    items: list[dict[str, Any]],
    key_fields: list[str],
    value_field: str = "qty",
) -> dict[tuple, float]:
    """按 key_fields 分组，对 value_field 求和。返回 {key_tuple: total}。"""
    result: dict[tuple, float] = {}
    for item in items:
        key = tuple(item.get(f) for f in key_fields)
        val = float(item.get(value_field, 0))
        result[key] = result.get(key, 0) + val
    return result


def _extract_day_of_week(d: str | Any) -> int:
    """返回日期的星期几（0=Monday, 6=Sunday）。"""
    if isinstance(d, str):
        return dt.date.fromisoformat(d).weekday()
    if hasattr(d, "weekday"):
        return d.weekday()
    return 0

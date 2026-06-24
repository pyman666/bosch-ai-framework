"""Mock BFF 数据 — 没有真实 BFF 时返回假数据，方便本地开发测试."""

from __future__ import annotations

import random
import logging

log = logging.getLogger(__name__)


def _mock_order_summary(params: dict) -> dict:
    """模拟订单汇总数据."""
    group_by = params.get("group_by", "channel")

    if group_by == "channel":
        data = [
            {"channel": "电商", "order_count": 3400, "gmv": 5100000, "avg_amount": 1500},
            {"channel": "直营", "order_count": 1200, "gmv": 2160000, "avg_amount": 1800},
            {"channel": "分销", "order_count": 800,  "gmv": 960000,  "avg_amount": 1200},
        ]
    elif group_by == "region":
        data = [
            {"region": "华东", "order_count": 2100, "gmv": 3780000, "avg_amount": 1800},
            {"region": "华南", "order_count": 1500, "gmv": 2250000, "avg_amount": 1500},
            {"region": "华北", "order_count": 1100, "gmv": 1540000, "avg_amount": 1400},
            {"region": "西南", "order_count": 700,  "gmv": 650000,  "avg_amount": 929},
        ]
    elif group_by in ("daily", "weekly", "monthly"):
        data = [
            {"date": "2026-05-01", "order_count": 150, "gmv": 225000, "avg_amount": 1500},
            {"date": "2026-05-02", "order_count": 132, "gmv": 198000, "avg_amount": 1500},
            {"date": "2026-05-03", "order_count": 178, "gmv": 267000, "avg_amount": 1500},
            {"date": "2026-05-04", "order_count": 165, "gmv": 247500, "avg_amount": 1500},
            {"date": "2026-05-05", "order_count": 198, "gmv": 297000, "avg_amount": 1500},
            {"date": "2026-05-06", "order_count": 210, "gmv": 315000, "avg_amount": 1500},
            {"date": "2026-05-07", "order_count": 188, "gmv": 282000, "avg_amount": 1500},
            {"date": "2026-05-08", "order_count": 220, "gmv": 330000, "avg_amount": 1500},
            {"date": "2026-05-09", "order_count": 195, "gmv": 292500, "avg_amount": 1500},
            {"date": "2026-05-10", "order_count": 175, "gmv": 262500, "avg_amount": 1500},
            {"date": "2026-05-11", "order_count": 160, "gmv": 240000, "avg_amount": 1500},
            {"date": "2026-05-12", "order_count": 205, "gmv": 307500, "avg_amount": 1500},
            {"date": "2026-05-13", "order_count": 230, "gmv": 345000, "avg_amount": 1500},
            {"date": "2026-05-14", "order_count": 185, "gmv": 277500, "avg_amount": 1500},
            {"date": "2026-05-15", "order_count": 210, "gmv": 315000, "avg_amount": 1500},
        ]
    else:
        data = [{"label": "汇总", "order_count": 5400, "gmv": 8220000, "avg_amount": 1522}]

    return {
        "success": True,
        "data": data,
        "meta": {
            "description": "订单量（不含已取消），GMV 为已支付金额",
            "time_range": f"{params.get('start_date', '?')} ~ {params.get('end_date', '?')}",
            "total": sum(r["order_count"] for r in data),
        },
    }


def _mock_order_detail(params: dict) -> dict:
    """模拟订单明细."""
    limit = int(params.get("limit", 10))
    channels = ["电商", "直营", "分销"]
    statuses = ["paid", "paid", "paid", "refunded", "cancelled"]  # 加权
    data = []
    for i in range(min(limit, 20)):
        data.append({
            "order_id": f"ORD-20260501-{i+1:03d}",
            "amount": random.choice([8800, 15600, 32000, 64000, 128000, 256000]),
            "channel": random.choice(channels),
            "user_id": f"U-{random.randint(10000, 99999)}",
            "status": random.choice(statuses),
            "created_at": f"2026-05-{random.randint(1, 31):02d}T{random.randint(8, 23):02d}:{random.randint(0, 59):02d}:00",
        })
    return {
        "success": True,
        "data": data,
        "meta": {"total": 5400, "description": "订单明细（含退款/取消）"},
    }


def _mock_user_metrics(params: dict) -> dict:
    """模拟用户指标."""
    metric = params.get("metric", "new_users")
    data = []
    base = {"new_users": 300, "dau": 15000, "wau": 45000, "mau": 120000}

    for day in range(1, 32):
        row = {"date": f"2026-05-{day:02d}"}
        if metric in ("new_users", "all"):
            row["new_users"] = base["new_users"] + random.randint(-50, 80)
        if metric in ("dau", "all"):
            row["dau"] = base["dau"] + random.randint(-1500, 2000)
        if metric in ("wau", "all"):
            row["wau"] = base["wau"] + random.randint(-3000, 4000)
        if metric in ("mau", "all"):
            row["mau"] = base["mau"] + random.randint(-5000, 8000)
        if metric == "retention":
            row["retention_7d"] = round(0.35 + random.uniform(-0.05, 0.05), 3)
            row["retention_30d"] = round(0.18 + random.uniform(-0.03, 0.03), 3)
        data.append(row)

    return {
        "success": True,
        "data": data,
        "meta": {
            "description": "新增用户为首次注册去重，DAU 为当日有任意行为的用户",
            "time_range": f"{params.get('start_date', '?')} ~ {params.get('end_date', '?')}",
        },
    }


def _mock_user_cohort(params: dict) -> dict:
    """模拟用户分群."""
    channels = ["电商", "直营", "分销"]
    data = []
    for ch in channels:
        base = random.randint(800, 2000)
        row = {
            "channel": ch,
            "cohort_size": base,
            "day_1_retained": int(base * random.uniform(0.4, 0.6)),
            "day_7_retained": int(base * random.uniform(0.2, 0.35)),
            "day_30_retained": int(base * random.uniform(0.1, 0.2)),
        }
        data.append(row)
    return {
        "success": True,
        "data": data,
        "meta": {"description": "按渠道分群的用户留存数据"},
    }


# 路由表
_MOCK_HANDLERS = {
    "query_order_summary": _mock_order_summary,
    "query_order_detail": _mock_order_detail,
    "query_user_metrics": _mock_user_metrics,
    "query_user_cohort": _mock_user_cohort,
}


def handle_mock(tool_name: str, params: dict) -> dict | None:
    """尝试用 mock 处理 tool 调用.

    返回:
        dict — mock 结果
        None — 不是 mock tool
    """
    handler = _MOCK_HANDLERS.get(tool_name)
    if handler is None:
        return None
    log.info(f"[mock] Handling tool call: {tool_name}({params})")
    return handler(params)

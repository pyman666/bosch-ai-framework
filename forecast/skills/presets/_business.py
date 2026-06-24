"""业务逻辑预设（模式 A~G）— fwdy_jitcall_priority、geely_monthly_daily_blend、fawvw_long_cycle、gac_ne_monthly_split、saic_daily_to_monthly_split、toyota_row_merge_freeze、ming_daily_order_blend。"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from forecast.skills.presets._helpers import parse_demand_series, build_date_qty_map


def group_days_by_iso_week(days: list[dict[str, Any]]) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """将日数据按 ISO 周分组。"""
    weeks: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for d in days:
        iso_year, iso_week, _ = d["date"].isocalendar()
        key = (iso_year, iso_week)
        weeks.setdefault(key, []).append(d)
    return weeks


def _group_days_by_month(days: list[dict[str, Any]]) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """将日数据按日历月分组（年, 月）。"""
    months: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for d in days:
        key = (d["date"].year, d["date"].month)
        months.setdefault(key, []).append(d)
    return months


def _running_balance(
    demand: list[dict[str, Any]],
    supply: dict[dt.date, float],
    begin_inv: float
) -> list[dict[str, Any]]:
    """计算运行库存余额。

    Parameters
    ----------
    demand : list[dict]
        需求序列，每个元素包含 date 和 qty 字段
    supply : dict[date, float]
        供应映射（INS/PGI），日期到数量的映射
    begin_inv : float
        期初库存

    Returns
    -------
    list[dict]
        包含 date、qty（净需求）、balance（余额）的列表
    """
    bal = begin_inv
    result = []
    for item in demand:
        dd = item["date"]
        inflow = supply.get(dd, 0.0)
        outflow = item["qty"]
        bal = bal + inflow - outflow
        net = max(0, -bal) if bal < 0 else 0.0
        result.append({
            "date": dd,
            "qty": round(float(net), 2),
            "balance": round(float(bal), 2)
        })
        bal = max(0, bal)  # 余额限制为非负数
    return result


def priority_fill(
    days: list[dict[str, Any]],
    primary_map: dict[dt.date, float],
    fallback_field: str = "qty",
) -> dict[dt.date, float]:
    """按优先级填充每日值：primary_map 中有正值则取它，否则取 day 的 fallback_field。"""
    result: dict[dt.date, float] = {}
    for d in days:
        dd = d["date"]
        if dd in primary_map and primary_map[dd] > 0:
            result[dd] = primary_map[dd]
        else:
            result[dd] = d[fallback_field]
    return result


def spread_remainder(
    daily_map: dict[dt.date, float],
    target: float,
    skip_dates: set[dt.date] | None = None,
) -> dict[dt.date, float]:
    """将 target 与当前总量的差额平摊到非跳过日期。

    若余量 <= 0 或无可平摊日期，返回 daily_map 的副本。
    """
    skip = skip_dates or set()
    current_total = sum(daily_map.values())
    remaining = target - current_total

    if remaining <= 0:
        return dict(daily_map)

    non_skip = [dd for dd in daily_map if dd not in skip]
    if not non_skip:
        return dict(daily_map)

    spread = remaining / len(non_skip)
    result = dict(daily_map)
    for dd in non_skip:
        result[dd] = result[dd] + spread
    return result


def _process_week_jitcall(
    week_days: list[dict[str, Any]],
    weekly_demand: float,
    jitcall_map: dict[dt.date, float],
    pgi_map: dict[dt.date, float],
) -> list[dict[str, Any]]:
    """处理单周 JITCall 优先级逻辑：JITCall 优先，余量平摊到无 JITCall 日期。"""
    # Step 1: 日需求1（JITCall 优先）
    daily_d1 = priority_fill(week_days, jitcall_map)

    # Step 2: 该周的 PGI 总量
    week_pgi_total = sum(pgi_map.get(d["date"], 0) for d in week_days)

    # Step 3-4: 余量平摊到无 JITCall 的日期
    jitcall_dates = {d for d in jitcall_map if jitcall_map[d] > 0}
    daily_d2 = spread_remainder(daily_d1, weekly_demand - week_pgi_total, skip_dates=jitcall_dates)

    # Step 5: 输出
    result = []
    for d in week_days:
        dd = d["date"]
        qty = max(0, round(float(daily_d2.get(dd, 0)), 2))
        result.append({"date": dd.isoformat(), "qty": qty})

    return result


def run_jitcall_priority(
    record: dict[str, Any],
    *,
    total_weekly: float,
    divide_by_weeks: bool = False,
) -> list[dict[str, Any]]:
    """JITCall 优先级通用编排：解析 demand → 按 ISO 周分组 → 逐周平摊。

    Parameters
    ----------
    divide_by_weeks : bool
        - True：模式 A（fwdy_jitcall_priority），将 total_weekly 按周数均分
        - False：模式 C（fawvw_long_cycle），直接使用 total_weekly
    """
    daily_demand_raw = record.get("demand", [])
    jitcall_raw = record.get("jitcall", [])
    pgi_raw = record.get("pgi", [])

    if not daily_demand_raw:
        return []

    days = parse_demand_series(daily_demand_raw)
    if not days:
        return []

    days.sort(key=lambda x: x["date"])

    jitcall_map = build_date_qty_map(jitcall_raw)
    pgi_map = build_date_qty_map(pgi_raw)

    weeks = group_days_by_iso_week(days)
    if divide_by_weeks:
        num_weeks = len(weeks)
        weekly_demand = total_weekly / num_weeks if num_weeks > 0 else total_weekly
    else:
        weekly_demand = total_weekly

    result = []
    for week_key in sorted(weeks.keys()):
        result.extend(_process_week_jitcall(
            week_days=weeks[week_key],
            weekly_demand=weekly_demand,
            jitcall_map=jitcall_map,
            pgi_map=pgi_map,
        ))

    return result


def fwdy_jitcall_priority(record: dict[str, Any]) -> list[dict[str, Any]]:
    """模式 A：周需求 + JITCall 优先级 + 日订单 → 日发货量（富维东阳 FWDY）。

    逻辑（对齐 FWDY 的 Excel）：
    1. JITCall 优先级最高：有 JITCall 的日期取 JITCall 值
    2. 无 JITCall 的日期取日需求值
    3. 余量 = 周需求 - PGI - Σ(每日需求)
    4. 余量 > 0 则均摊到无 JITCall 的日期；余量 <= 0 则不分配
    5. 日发货量 = 日需求1 + 余量均摊（最小为 0）

    跨周支持：如果 demand 跨越多周，按 ISO 周分组，每周独立计算余量平摊。
    weekly_demand 为单值时按周数均分（与模式 C FAW-VW 的关键区别）。
    """
    return run_jitcall_priority(
        record,
        total_weekly=float(record.get("weekly_demand", 0)),
        divide_by_weeks=True,
    )


def geely_monthly_daily_blend(record: dict[str, Any]) -> list[dict[str, Any]]:
    """模式 B：月预测 + 日需求整合 + 库存 Balance → 净需求（日发货量）

    对齐 Geely / Xpeng Excel (Logic sample) 的逐月算法：
    1. 日需求和月预测整合（逐月独立计算 fill rate）：
       - 当月有需求的日期 → 取日需求值（0 值算需求）
       - 当月无需求的日期 → fill_rate = (月预测 − 当月需求合计) / 当月剩余天数
       - 日期轴从最早需求日（或首月 1 号，取早者）到末月最后一天
       - 无需求且无月预测的日期（预预测期）→ 有 demand 取 demand，否则 0
    2. 库存 Balance：Balance[i] = Balance[i-1] + INS/PGI[i] - 整合需求[i]
    3. 净需求：当 Balance < 0 时取 |Balance|，否则为 0
    4. 日发货量 = 净需求

    monthly_forecast 支持两种格式：
    - float：向后兼容，应用于 first_day 所在月份（单月模式）
    - dict：{"2026-01": 8014, "2026-02": 5471} 多月模式（对齐 Excel）
    """
    monthly_forecast_raw = record.get("monthly_forecast", 0)
    daily_demand_raw = record.get("demand", [])
    pgi_raw = record.get("pgi", [])
    ins_raw = record.get("ins", [])
    begin_inv = float(record.get("beginningInventory", record.get("beginning_inventory", 0)))

    days = parse_demand_series(daily_demand_raw)
    days.sort(key=lambda x: x["date"])
    demand_map: dict[dt.date, float] = {d["date"]: d["qty"] for d in days}

    # ---- Step 0: 解析月预测 → { "YYYY-MM": float } ----
    forecasts: dict[str, float] = {}
    if isinstance(monthly_forecast_raw, dict):
        for k, v in monthly_forecast_raw.items():
            forecasts[k] = float(v)
    elif monthly_forecast_raw:
        # 单月 float 模式：需确定月份
        if not days:
            return []
        first_day = days[0]["date"]
        month_key = f"{first_day.year}-{first_day.month:02d}"
        forecasts[month_key] = float(monthly_forecast_raw)
    else:
        # monthly_forecast <= 0 且无 dict：fallback 到 demand 日均值（仅单月）
        if not days:
            return []
        available = [d["qty"] for d in days if d["qty"] > 0]
        fallback = sum(available) / len(available) if available else 0
        if fallback <= 0:
            return []
        first_day = days[0]["date"]
        month_key = f"{first_day.year}-{first_day.month:02d}"
        forecasts[month_key] = fallback

    if not forecasts:
        return []

    # ---- Step 1: 确定日期轴范围 ----
    sorted_months = sorted(forecasts.keys())
    first_month_str = sorted_months[0]
    last_month_str = sorted_months[-1]

    first_month_start = dt.date.fromisoformat(first_month_str + "-01")
    last_y, last_m = map(int, last_month_str.split("-"))
    last_month_end = dt.date(last_y, last_m, calendar.monthrange(last_y, last_m)[1])

    # 起点取更早者：earliest demand 或首月 1 号
    if days:
        start_date = min(days[0]["date"], first_month_start)
    else:
        start_date = first_month_start

    num_days = (last_month_end - start_date).days + 1
    all_dates = [start_date + dt.timedelta(days=i) for i in range(num_days)]

    # ---- Step 2: 逐月计算 fill_rate ----
    month_fill_rate: dict[str, float] = {}
    for month_key in sorted_months:
        y, m = map(int, month_key.split("-"))
        forecast = forecasts[month_key]
        month_total_days = calendar.monthrange(y, m)[1]

        # 该月内 demand 合计
        month_demand_sum = 0.0
        month_demand_days = 0
        for d, qty in demand_map.items():
            if d.year == y and d.month == m:
                month_demand_sum += qty
                month_demand_days += 1

        remaining_days = month_total_days - month_demand_days
        if remaining_days > 0:
            fill = (forecast - month_demand_sum) / remaining_days
        else:
            fill = 0.0
        month_fill_rate[month_key] = fill

    # ---- Step 3: 构建整合需求 blended_demand ----
    blended_demand: list[dict[str, Any]] = []
    for dd in all_dates:
        month_key = f"{dd.year}-{dd.month:02d}"
        if dd in demand_map:
            blended_demand.append({"date": dd, "qty": demand_map[dd]})
        elif month_key in month_fill_rate:
            blended_demand.append({"date": dd, "qty": month_fill_rate[month_key]})
        else:
            # 预预测期（第一个预测月之前）：demand 已处理，无 forecast → 0
            blended_demand.append({"date": dd, "qty": 0.0})

    # ---- Step 4: INS/PGI 解析 ----
    ins_items = ins_raw if ins_raw else pgi_raw
    ins_map = build_date_qty_map(ins_items)

    # ---- Step 5: 使用 _running_balance 计算库存余额 ----
    result_dicts = _running_balance(blended_demand, ins_map, begin_inv)

    # 转换为字符串日期格式以保持向后兼容
    result = [
        {
            "date": item["date"].isoformat(),
            "qty": item["qty"],
            "balance": item["balance"]
        }
        for item in result_dicts
    ]

    return result


def fawvw_long_cycle(record: dict[str, Any]) -> list[dict[str, Any]]:
    """模式 C：FAW-VW 长周期需求预测（一汽大众 FAW-VW 各工厂）。

    使用 weekly_demand 作为总周需求（调用方已合并多数据源），
    日计划(demand) 作为基础需求序列，JITCall 最高优先级。

    与模式 A（富维东阳 FWDY）的区别：weekly_demand 不按周数均分，
    直接作为每周的需求量。

    逻辑：
    1. total_weekly = weekly_demand
    2. 将 demand 按 ISO 周分组
    3. 对每一周应用 JITCall 优先级 + 余量平摊
    4. 合并所有周的结果
    """
    return run_jitcall_priority(
        record,
        total_weekly=float(record.get("weekly_demand", 0) or 0),
        divide_by_weeks=False,
    )


def gac_ne_monthly_split(record: dict[str, Any]) -> list[dict[str, Any]]:
    """GAC-NE Legacy: 月预测拆 6 个月 + 当月扣减已交货量。

    逻辑（对齐 forecast-gacne-calculation.md）：
    1. currentMonth 自动 +1 个月
    2. 将 forecastFirstNum ~ forecastSixthNum 拆分为 6 个月的 ForecastEntity
    3. 当月（currentMonth）扣减 deliveryCount（最低到 0）
    4. 只保留当月 Forecast，其余月份移除
    """
    current_month_raw = int(record.get("current_month", 202601))
    forecast_nums = [
        float(record.get("forecast_first_num", 0)),
        float(record.get("forecast_second_num", 0)),
        float(record.get("forecast_third_num", 0)),
        float(record.get("forecast_fourth_num", 0)),
        float(record.get("forecast_fifth_num", 0)),
        float(record.get("forecast_sixth_num", 0)),
    ]
    delivery_count = float(record.get("delivery_count", 0))

    # Step 1: currentMonth +1
    year = current_month_raw // 100
    month = current_month_raw % 100
    if month == 12:
        year += 1
        month = 1
    else:
        month += 1

    # Step 2: 拆分 6 个月 ForecastEntity
    all_months = []
    current_month_key = f"{year}{month:02d}"

    for i in range(6):
        m = month + i
        y = year
        while m > 12:
            m -= 12
            y += 1
        month_key = f"{y}{m:02d}"
        count = forecast_nums[i]

        # Step 3: 当月扣减已交货量
        if month_key == current_month_key:
            count = max(0.0, count - delivery_count)

        all_months.append({"date": month_key, "qty": count, "type": "monthly"})

    # Step 4: 只保留当月
    result = [r for r in all_months if r["date"] == current_month_key]
    return result


def saic_daily_to_monthly_split(record: dict[str, Any]) -> list[dict[str, Any]]:
    """SAIC-KD/SAIC-NON-KD: 日需求按日期合并 → 日度明细 + 月度汇总。

    逻辑（对齐 forecast-saickd-calculation.md 和 forecast-saicnonkd-calculation.md）：
    1. 按 demandDate 分组求和（mergeCountByDate，SAIC-NON-KD 跳过此步）
    2. 生成日度明细 ForecastDailyDetailEntity
    3. 按 yyyyMM 分组求和生成月度明细 ForecastMonthlyDetailEntity
    """
    demand_raw = record.get("demand", [])
    merge_by_date = record.get("merge_by_date", True)

    if not demand_raw:
        return []

    days = parse_demand_series(demand_raw)
    if not days:
        return []

    result = []

    # Step 1: 按日期合并（SAIC-KD 有 mergeCountByDate，SAIC-NON-KD 无）
    if merge_by_date:
        daily_map: dict[dt.date, float] = {}
        for d in days:
            dd = d["date"]
            daily_map[dd] = daily_map.get(dd, 0) + d["qty"]

        # Step 2: 生成日度明细
        for date_key in sorted(daily_map.keys()):
            qty = daily_map[date_key]
            date_str = date_key.strftime("%Y%m%d")
            result.append({"date": date_str, "qty": qty, "type": "daily"})

        # Step 3: 按 yyyyMM 分组求和生成月度明细
        monthly_map: dict[str, float] = {}
        for date_key, qty in daily_map.items():
            month_key = date_key.strftime("%Y%m")
            monthly_map[month_key] = monthly_map.get(month_key, 0) + qty

        for month_key in sorted(monthly_map.keys()):
            result.append({"date": month_key, "qty": monthly_map[month_key], "type": "monthly"})
    else:
        # 不合并：保留每条原始日度数据，同时生成月度汇总
        for d in sorted(days, key=lambda x: x["date"]):
            date_str = d["date"].strftime("%Y%m%d")
            result.append({"date": date_str, "qty": d["qty"], "type": "daily"})

        # 月度汇总
        monthly_map: dict[str, float] = {}
        for d in days:
            month_key = d["date"].strftime("%Y%m")
            monthly_map[month_key] = monthly_map.get(month_key, 0) + d["qty"]

        for month_key in sorted(monthly_map.keys()):
            result.append({"date": month_key, "qty": monthly_map[month_key], "type": "monthly"})

    return result


def toyota_row_merge_freeze(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Toyota GTMC: 行合并 + 同日期冻结汇总。

    逻辑（对齐 forecast-toyota-calculation.md）：
    1. RowMerge: 按 Plant + SupplierCode + PartNo + Date 分组合并
    2. Freeze: 同日期/月份的 ExtendEntity 数量求和
    3. 去重：如果某月有日度数据，则移除该月的月度数据
    """
    rows = record.get("rows", [])

    if not rows:
        return []

    # Step 1: RowMerge - 按 plant+supplier+partNo+date 分组
    merged: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        plant = row.get("plant", "")
        supplier = row.get("supplier_code", "")
        part_no = row.get("part_no", "")
        date = row.get("date", "")

        key = (plant, supplier, part_no, date)
        merged.setdefault(key, []).append(row)

    result = []

    # Step 2: Freeze - 同日期/月份数量求和
    daily_map: dict[str, float] = {}
    monthly_map: dict[str, float] = {}
    has_daily: dict[str, set] = {}  # month_key -> set of day strings

    for key, row_list in merged.items():
        date = key[3]

        # 累加日度数据
        daily_qty = sum(float(r.get("qty", 0)) for r in row_list)
        if daily_qty > 0:
            date_str = date if isinstance(date, str) else date.strftime("%Y%m%d")
            daily_map[date_str] = daily_map.get(date_str, 0) + daily_qty

            # 记录该月有日度数据
            month_key = date_str[:6] if len(date_str) == 8 else date_str[:7].replace("-", "")
            has_daily.setdefault(month_key, set()).add(date_str)

        # 累加月度数据
        monthly_qty = sum(float(r.get("monthly_qty", 0)) for r in row_list)
        if monthly_qty > 0:
            date_str = date if isinstance(date, str) else date.strftime("%Y%m%d")
            month_key = date_str[:6] if len(date_str) == 8 else date_str[:7].replace("-", "")
            monthly_map[month_key] = monthly_map.get(month_key, 0) + monthly_qty

    # 输出去重：如果某月有日度数据，则移除该月的月度数据
    for date_str in sorted(daily_map.keys()):
        result.append({"date": date_str, "qty": daily_map[date_str], "type": "daily"})

    for month_key in sorted(monthly_map.keys()):
        if month_key not in has_daily or not has_daily[month_key]:
            result.append({"date": month_key, "qty": monthly_map[month_key], "type": "monthly"})

    return result


def ming_daily_order_blend(record: dict[str, Any]) -> list[dict[str, Any]]:
    """名辰 Ming 日订单 + 预测缺口补足 → 日发货量.

    逻辑（对齐 Ming.xlsx Logic sample）：
    1. 日订单优先：有日订单的日期取日订单值
    2. 缺口计算：预测 − PGI 合计 − 日订单合计
    3. 缺口平摊：
       - 周度模式：缺口全部放到周日（Case 2）
       - 月度模式：缺口从最后一个日订单日起平摊到月末（Case 6）
    4. 如果日订单已覆盖到周期末（周日/月末）→ 直接取日订单，不平摊

    入参 record 字段：
    - forecast_type: "weekly" | "monthly"
    - forecast: float（预测总量）
    - daily_orders: [{date, qty}]（日订单）
    - pgi: [{date, qty}]（已发货在途）
    - transportation_lt: int（运输提前期，仅记录用）
    """
    forecast_type = record.get("forecast_type", "weekly")
    forecast = float(record.get("forecast", 0))
    daily_orders_raw = record.get("daily_orders", [])
    pgi_raw = record.get("pgi", [])

    # 解析日订单
    orders: dict[dt.date, float] = {}
    for item in daily_orders_raw:
        d = dt.date.fromisoformat(item["date"]) if isinstance(item["date"], str) else item["date"]
        orders[d] = orders.get(d, 0) + float(item.get("qty", 0))

    if not orders:
        return []

    order_dates = sorted(orders.keys())
    first_date = order_dates[0]
    last_order_date = order_dates[-1]

    # 确定周期边界
    if forecast_type == "monthly":
        month_end = dt.date(first_date.year, first_date.month,
                            calendar.monthrange(first_date.year, first_date.month)[1])
        period_end = month_end
        period_start = first_date
    else:
        # 周度：周一 ~ 周日
        weekday = first_date.weekday()  # 0=Mon
        period_start = first_date - dt.timedelta(days=weekday)
        period_end = period_start + dt.timedelta(days=6)

    # 生成完整周期日期轴
    num_days = (period_end - period_start).days + 1
    all_dates = [period_start + dt.timedelta(days=i) for i in range(num_days)]

    # PGI 合计
    pgi_total = sum(float(item.get("qty", 0)) for item in pgi_raw)
    order_total = sum(orders.values())

    # 日订单已覆盖到周期末 → 直接用订单
    if last_order_date >= period_end:
        result: list[dict[str, Any]] = []
        for dd in all_dates:
            result.append({"date": dd.isoformat(),
                           "qty": round(orders.get(dd, 0.0), 2),
                           "type": "daily_order"})
        return result

    # 缺口平摊
    if forecast_type == "monthly":
        # 月度：forecast 是剩余量，直接从最后订单日平摊到月末
        spread_dates = [d for d in all_dates if d >= last_order_date]
        spread_amount = forecast  # 月度 forecast 已是净剩余，不扣 PGI/orders
    else:
        # 周度：缺口 = 预测 − PGI − 订单，全部放到周日
        gap = forecast - pgi_total - order_total
        spread_dates = [period_end]
        spread_amount = gap

    spread_per_day = spread_amount / len(spread_dates) if spread_dates else 0.0

    result = []
    for dd in all_dates:
        if dd in spread_dates:
            result.append({"date": dd.isoformat(),
                           "qty": round(spread_per_day, 2),
                           "type": "spread"})
        elif dd in orders:
            result.append({"date": dd.isoformat(),
                           "qty": round(orders[dd], 2),
                           "type": "daily_order"})
        else:
            result.append({"date": dd.isoformat(),
                           "qty": 0.0,
                           "type": "empty"})

    return result

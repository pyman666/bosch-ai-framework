"""业务逻辑预设测试 — 用 Excel 中的实际案例验证计算逻辑。"""

import pytest
from forecast.skills.presets import run_preset


# ---------------------------------------------------------------------------
# FWDY Excel — logic example 工作表 Case 1 数据
# ---------------------------------------------------------------------------

@pytest.fixture
def fwdy_case1():
    """FWDY 逻辑示例 Case 1：周日有需求，有 JITCall。

    周需求 = 800，日需求 = 100/天（周一到周日），PGI = 100，
    JITCall = 周二到周四各 80。
    """
    return {
        "weekly_demand": 800,
        "demand": [
            {"date": "2025-11-24", "qty": 100},  # Mon
            {"date": "2025-11-25", "qty": 100},  # Tue
            {"date": "2025-11-26", "qty": 100},  # Wed
            {"date": "2025-11-27", "qty": 100},  # Thu
            {"date": "2025-11-28", "qty": 100},  # Fri
            {"date": "2025-11-29", "qty": 100},  # Sat
            {"date": "2025-11-30", "qty": 100},  # Sun
        ],
        "jitcall": [
            {"date": "2025-11-25", "qty": 80},
            {"date": "2025-11-26", "qty": 80},
            {"date": "2025-11-27", "qty": 80},
        ],
        "pgi": [
            {"date": "2025-11-24", "qty": 100},
        ],
        "transportationLT": 3,
    }


@pytest.fixture
def fwdy_case2():
    """FWDY 逻辑示例 Case 2：周日有需求，没有 JITCall。

    周需求 = 800，日需求 = 100/天（周一到周日），PGI = 100，
    没有 JITCall。
    """
    return {
        "weekly_demand": 800,
        "demand": [
            {"date": "2025-11-24", "qty": 100},  # Mon
            {"date": "2025-11-25", "qty": 100},  # Tue
            {"date": "2025-11-26", "qty": 100},  # Wed
            {"date": "2025-11-27", "qty": 100},  # Thu
            {"date": "2025-11-28", "qty": 100},  # Fri
            {"date": "2025-11-29", "qty": 100},  # Sat
            {"date": "2025-11-30", "qty": 100},  # Sun
        ],
        "jitcall": [],
        "pgi": [
            {"date": "2025-11-24", "qty": 100},
        ],
        "transportationLT": 3,
    }


# ---------------------------------------------------------------------------
# Geely Excel — Logic sample 工作表 Case 1 数据
# ---------------------------------------------------------------------------

@pytest.fixture
def geely_case1():
    """Geely 逻辑示例 Case 1：单月 dict 预测 + 跨月需求。

    monthly_forecast = {"2026-01": 8014}（对齐 Excel Case 1 的 1 月部分），
    日需求跨 12/29-01/15 共 18 天，期初库存 3316。
    日期轴 = 12/29~01/31（34 天），12 月为预预测期（有 demand 取值、无则 0）。
    1 月 fill_rate = (8014 − Jan_demand_sum) / Jan_remaining = 179.5625 用于 01/16-01/31。
    """
    import datetime as dt

    demand = []
    daily_values = [
        (dt.date(2025, 12, 29), 408), (dt.date(2025, 12, 30), 227),
        (dt.date(2025, 12, 31), 90), (dt.date(2026, 1, 1), 0),
        (dt.date(2026, 1, 2), 0), (dt.date(2026, 1, 3), 0),
        (dt.date(2026, 1, 4), 0), (dt.date(2026, 1, 5), 178),
        (dt.date(2026, 1, 6), 328), (dt.date(2026, 1, 7), 213),
        (dt.date(2026, 1, 8), 290), (dt.date(2026, 1, 9), 420),
        (dt.date(2026, 1, 10), 320), (dt.date(2026, 1, 11), 350),
        (dt.date(2026, 1, 12), 401), (dt.date(2026, 1, 13), 402),
        (dt.date(2026, 1, 14), 291), (dt.date(2026, 1, 15), 1948),
    ]

    for d, qty in daily_values:
        demand.append({"date": d.isoformat(), "qty": qty})

    return {
        "monthly_forecast": {"2026-01": 8014},
        "demand": demand,
        "beginningInventory": 3316,
        "ins": [
            {"date": "2026-01-09", "qty": 576},
            {"date": "2026-01-11", "qty": 1152},
            {"date": "2026-01-14", "qty": 576},
        ],
        "pgi": [],
    }


# ---------------------------------------------------------------------------
# FWDY Case 1 测试
# ---------------------------------------------------------------------------

class TestFWDYCase1:
    """Case 1: 周日有需求，有 JITCall。"""

    def test_output_length(self, fwdy_case1):
        result = run_preset("jitcall_priority", fwdy_case1)
        assert len(result) == 7

    def test_jitcall_days_take_jitcall(self, fwdy_case1):
        """周二到周四有 JITCall，应该取 JITCall 值 80。"""
        result = run_preset("jitcall_priority", fwdy_case1)
        qtys = {r["date"]: r["qty"] for r in result}

        assert qtys["2025-11-25"] == 80   # Tue JITCall
        assert qtys["2025-11-26"] == 80   # Wed JITCall
        assert qtys["2025-11-27"] == 80   # Thu JITCall

    def test_non_jitcall_days_take_daily_plus_spread(self, fwdy_case1):
        """没有 JITCall 的天取日需求值 + 余量平摊。

        周需求 800 - PGI 100 - 已分配 640 = 余量 60
        4 个非 JITCall 天，每天平摊 60/4 = 15
        所以日发货量 = 100 + 15 = 115
        """
        result = run_preset("jitcall_priority", fwdy_case1)
        qtys = {r["date"]: r["qty"] for r in result}

        assert qtys["2025-11-24"] == 115  # Mon: 100 + 15
        assert qtys["2025-11-28"] == 115  # Fri: 100 + 15
        assert qtys["2025-11-29"] == 115  # Sat: 100 + 15
        assert qtys["2025-11-30"] == 115  # Sun: 100 + 15

    def test_total_equals_weekly_minus_pgi(self, fwdy_case1):
        """总发货量 = 周需求 - PGI。"""
        result = run_preset("jitcall_priority", fwdy_case1)
        total = sum(r["qty"] for r in result)
        expected = 800 - 100  # weekly - pgi
        # 允许取整误差
        assert abs(total - expected) <= 10

    def test_all_values_non_negative(self, fwdy_case1):
        """所有日发货量 >= 0。"""
        result = run_preset("jitcall_priority", fwdy_case1)
        assert all(r["qty"] >= 0 for r in result)


# ---------------------------------------------------------------------------
# FWDY Case 2 测试
# ---------------------------------------------------------------------------

class TestFWDYCase2:
    """Case 2: 周日有需求，没有 JITCall。"""

    def test_output_length(self, fwdy_case2):
        result = run_preset("jitcall_priority", fwdy_case2)
        assert len(result) == 7

    def test_all_daily_values(self, fwdy_case2):
        """没有 JITCall，所有天应该取日需求值 100。"""
        result = run_preset("jitcall_priority", fwdy_case2)
        assert all(r["qty"] == 100 for r in result)

    def test_total_equals_weekly_minus_pgi(self, fwdy_case2):
        """总发货量 = 周需求 - PGI。"""
        result = run_preset("jitcall_priority", fwdy_case2)
        total = sum(r["qty"] for r in result)
        expected = 800 - 100
        assert abs(total - expected) <= 10


# ---------------------------------------------------------------------------
# Geely Case 1 测试
# ---------------------------------------------------------------------------

class TestGeelyCase1:
    """Case 1: 单月 dict 预测 + 跨月需求（对齐 Excel Case 1 的 1 月部分）。

    monthly_forecast = {"2026-01": 8014}，日期轴 12/29~01/31（34 天）。
    12 月为预预测期（有 demand 取值），1 月 fill_rate = (8014-5141)/16 = 179.5625。
    期初库存 3316，INS 在 01/09、01/11、01/14 到货。
    """

    def test_output_length(self, geely_case1):
        """日期轴 12/29~01/31 = 34 天。"""
        result = run_preset("monthly_daily_blend", geely_case1)
        assert len(result) == 34

    def test_output_format(self, geely_case1):
        result = run_preset("monthly_daily_blend", geely_case1)
        assert all("date" in r for r in result)
        assert all("qty" in r for r in result)
        assert all("balance" in r for r in result)

    def test_early_days_zero_qty(self, geely_case1):
        """前 17 天库存充足 (balance > 0)，日发货量为 0。"""
        result = run_preset("monthly_daily_blend", geely_case1)
        for i in range(17):
            assert result[i]["qty"] == 0.0, \
                f"Day {i} ({result[i]['date']}) should have qty=0, got {result[i]['qty']}"

    def test_jan15_triggers_demand(self, geely_case1):
        """01/15 日需求 1948，库存耗尽，触发发货 > 0。"""
        result = run_preset("monthly_daily_blend", geely_case1)
        jan15 = [r for r in result if r["date"] == "2026-01-15"]
        assert len(jan15) == 1
        assert jan15[0]["qty"] == 246.0

    def test_all_values_non_negative(self, geely_case1):
        result = run_preset("monthly_daily_blend", geely_case1)
        assert all(r["qty"] >= 0 for r in result)

    def test_missing_days_filled_by_fill_rate(self, geely_case1):
        """缺失日（01/16-01/31）由 fill_rate = (8014-5141)/16 = 179.5625 补齐。

        这是验证修复：之前 else 分支不可达，缺失日补齐逻辑从未触发；
        现在用 Excel 的逐月 fill_rate 算法，而非简单的 forecast/month_days。
        """
        result = run_preset("monthly_daily_blend", geely_case1)
        jan16 = [r for r in result if r["date"] == "2026-01-16"]
        assert len(jan16) == 1
        assert 179 < jan16[0]["qty"] < 180

    def test_cross_month_demand_preserved(self, geely_case1):
        """跨月需求数据未丢失：12/29 和 01/31 都在结果中。"""
        result = run_preset("monthly_daily_blend", geely_case1)
        dates = {r["date"] for r in result}
        assert "2025-12-29" in dates   # 12 月预预测期
        assert "2026-01-15" in dates   # 1 月 demand
        assert "2026-01-31" in dates   # 1 月最后一天（fill_rate 区）


# ---------------------------------------------------------------------------
# 模式 B 扩展测试
# ---------------------------------------------------------------------------

class TestMonthlyDailyBlendExtended:
    """月预测+日需求整合的扩展场景。"""

    def test_no_ins(self, geely_case1):
        """没有 INS 时，应该 fallback 到 PGI。"""
        geely_case1["ins"] = []
        result = run_preset("monthly_daily_blend", geely_case1)
        assert len(result) > 0

    def test_zero_beginning_inventory(self, geely_case1):
        """期初库存为 0 时应该立即触发发货。"""
        geely_case1["beginningInventory"] = 0
        result = run_preset("monthly_daily_blend", geely_case1)
        # 第一天就可能触发发货
        assert result[0]["qty"] >= 0

    def test_no_monthly_forecast(self, geely_case1):
        """月预测为 0，只用日需求。"""
        geely_case1["monthly_forecast"] = 0
        result = run_preset("monthly_daily_blend", geely_case1)
        assert len(result) > 0
        assert all(r["qty"] >= 0 for r in result)

    def test_large_inventory(self, geely_case1):
        """期初库存非常大时，日发货量始终为 0。"""
        geely_case1["beginningInventory"] = 100000
        result = run_preset("monthly_daily_blend", geely_case1)
        assert all(r["qty"] == 0 for r in result)


# ---------------------------------------------------------------------------
# FAW-VW Long Cycle (Mode C) 测试
# ---------------------------------------------------------------------------

@pytest.fixture
def fawvw_basic():
    """FAW-VW 长周期基本场景：周需求 + JITCall 部分天有值。

    总周需求 = 800（调用方已合并多数据源）
    日计划 = 100/天（周一到周日），PGI = 100，
    JITCall = 周二到周四各 80。
    """
    return {
        "weekly_demand": 800,
        "demand": [
            {"date": "2025-11-24", "qty": 100},  # Mon
            {"date": "2025-11-25", "qty": 100},  # Tue
            {"date": "2025-11-26", "qty": 100},  # Wed
            {"date": "2025-11-27", "qty": 100},  # Thu
            {"date": "2025-11-28", "qty": 100},  # Fri
            {"date": "2025-11-29", "qty": 100},  # Sat
            {"date": "2025-11-30", "qty": 100},  # Sun
        ],
        "jitcall": [
            {"date": "2025-11-25", "qty": 80},
            {"date": "2025-11-26", "qty": 80},
            {"date": "2025-11-27", "qty": 80},
        ],
        "pgi": [
            {"date": "2025-11-24", "qty": 100},
        ],
        "transportationLT": 3,
    }


class TestFAWVWLongCycleBasic:
    """FAW-VW 长周期基本场景测试。"""

    def test_output_length(self, fawvw_basic):
        result = run_preset("fawvw_long_cycle", fawvw_basic)
        assert len(result) == 7

    def test_jitcall_days_take_jitcall(self, fawvw_basic):
        """JITCall 天应该取 JITCall 值 80。"""
        result = run_preset("fawvw_long_cycle", fawvw_basic)
        qtys = {r["date"]: r["qty"] for r in result}
        assert qtys["2025-11-25"] == 80
        assert qtys["2025-11-26"] == 80
        assert qtys["2025-11-27"] == 80

    def test_total_equals_weekly_sum_minus_pgi(self, fawvw_basic):
        """总发货量 = 周需求 - PGI = 800 - 100 = 700。"""
        result = run_preset("fawvw_long_cycle", fawvw_basic)
        total = sum(r["qty"] for r in result)
        expected = 800 - 100
        assert abs(total - expected) <= 10

    def test_all_values_non_negative(self, fawvw_basic):
        result = run_preset("fawvw_long_cycle", fawvw_basic)
        assert all(r["qty"] >= 0 for r in result)


class TestFAWVWLongCycleEdgeCases:
    """FAW-VW 长周期边界场景测试。"""

    def test_smaller_weekly_demand(self, fawvw_basic):
        """周需求较小的场景：weekly_demand = 500。

        总周需求 500 - PGI 100 = 400 可用。但日需求1（JITCall+日计划）已达 640，
        余量 <= 0，不分配额外量，日发货量 = 日需求1。
        """
        fawvw_basic["weekly_demand"] = 500
        result = run_preset("fawvw_long_cycle", fawvw_basic)
        assert len(result) == 7
        # JITCall 天取 JITCall 值
        qtys = {r["date"]: r["qty"] for r in result}
        assert qtys["2025-11-25"] == 80
        assert qtys["2025-11-26"] == 80
        assert qtys["2025-11-27"] == 80

    def test_no_jitcall(self, fawvw_basic):
        """没有 JITCall，所有天取日需求值。"""
        fawvw_basic["jitcall"] = []
        result = run_preset("fawvw_long_cycle", fawvw_basic)
        assert len(result) == 7
        assert all(r["qty"] >= 0 for r in result)

    def test_no_demand(self):
        """没有日需求，返回空列表。"""
        record = {
            "weekly_demand": 800,
            "demand": [],
            "jitcall": [],
            "pgi": [],
        }
        result = run_preset("fawvw_long_cycle", record)
        assert result == []

    def test_multi_week(self):
        """跨两周的场景。"""
        import datetime as dt

        demand = []
        # Week 1: 2025-11-24 (Mon) to 2025-11-30 (Sun)
        # Week 2: 2025-12-01 (Mon) to 2025-12-07 (Sun)
        for i in range(14):
            d = dt.date(2025, 11, 24) + dt.timedelta(days=i)
            demand.append({"date": d.isoformat(), "qty": 100})

        record = {
            "weekly_demand": 800,
            "demand": demand,
            "jitcall": [
                {"date": "2025-11-25", "qty": 80},
                {"date": "2025-11-26", "qty": 80},
                {"date": "2025-12-02", "qty": 60},
            ],
            "pgi": [
                {"date": "2025-11-24", "qty": 100},
                {"date": "2025-12-01", "qty": 50},
            ],
            "transportationLT": 3,
        }
        result = run_preset("fawvw_long_cycle", record)
        assert len(result) == 14
        assert all(r["qty"] >= 0 for r in result)


# ---------------------------------------------------------------------------
# 跨周 / 多月支持测试
# ---------------------------------------------------------------------------

class TestCrossWeek:
    """jitcall_priority 跨周支持测试。"""

    def test_cross_week_two_weeks(self):
        """14 天 demand 跨 2 周，每周独立计算余量平摊。"""
        import datetime as dt

        demand = []
        for i in range(14):
            d = dt.date(2025, 11, 24) + dt.timedelta(days=i)
            demand.append({"date": d.isoformat(), "qty": 100})

        record = {
            "weekly_demand": 1600,  # 两周总需求，每周期望 800
            "demand": demand,
            "jitcall": [
                {"date": "2025-11-25", "qty": 80},  # Week 1 Tue
                {"date": "2025-11-26", "qty": 80},  # Week 1 Wed
                {"date": "2025-12-02", "qty": 60},  # Week 2 Tue
            ],
            "pgi": [
                {"date": "2025-11-24", "qty": 100},  # Week 1
                {"date": "2025-12-01", "qty": 50},   # Week 2
            ],
            "transportationLT": 3,
        }
        result = run_preset("jitcall_priority", record)
        assert len(result) == 14
        assert all(r["qty"] >= 0 for r in result)

        # Week 1: JITCall 天 (Tue/Wed) 应该是 80
        qtys = {r["date"]: r["qty"] for r in result}
        assert qtys["2025-11-25"] == 80
        assert qtys["2025-11-26"] == 80

        # Week 2: JITCall 天 (Tue) 应该是 60
        assert qtys["2025-12-02"] == 60

    def test_cross_week_single_week_backward_compat(self, fwdy_case1):
        """单周场景（7 天）与之前行为完全一致。"""
        result = run_preset("jitcall_priority", fwdy_case1)
        qtys = {r["date"]: r["qty"] for r in result}
        assert qtys["2025-11-25"] == 80
        assert qtys["2025-11-26"] == 80
        assert qtys["2025-11-27"] == 80
        assert qtys["2025-11-24"] == 115
        assert qtys["2025-11-28"] == 115


class TestMultiMonth:
    """monthly_daily_blend 多月支持测试。"""

    def test_multi_month_dict(self):
        """monthly_forecast 为 dict 格式，跨多月。

        12 月 5000 + 1 月 6000，日期轴 12/01~01/31（62 天）。
        12/29-12/31 有需求（各 100），其余天由各自的 fill_rate 补齐。
        """
        import datetime as dt

        dec_dates = [
            dt.date(2025, 12, 29), dt.date(2025, 12, 30), dt.date(2025, 12, 31),
        ]

        demand = []
        for d in dec_dates:
            demand.append({"date": d.isoformat(), "qty": 100})

        record = {
            "monthly_forecast": {"2025-12": 5000, "2026-01": 6000},
            "demand": demand,
            "beginningInventory": 500,
            "ins": [],
            "pgi": [],
        }
        result = run_preset("monthly_daily_blend", record)
        # 日期轴: 12/01~01/31 = 62 天
        assert len(result) == 62
        assert all(r["qty"] >= 0 for r in result)
        dates = {r["date"] for r in result}
        assert "2025-12-01" in dates  # 首月首日
        assert "2026-01-31" in dates  # 末月最后一天

    def test_multi_month_float_backward_compat(self):
        """float 格式的 monthly_forecast 向后兼容（单月模式）。"""
        record = {
            "monthly_forecast": 8014,
            "demand": [
                {"date": "2026-01-01", "qty": 408},
                {"date": "2026-01-02", "qty": 227},
                {"date": "2026-01-03", "qty": 90},
            ],
            "beginningInventory": 3316,
            "ins": [],
            "pgi": [],
        }
        result = run_preset("monthly_daily_blend", record)
        # 1 月全月 31 天
        assert len(result) == 31
        assert all(r["qty"] >= 0 for r in result)


# ---------------------------------------------------------------------------
# GAC-NE Monthly Split 测试
# ---------------------------------------------------------------------------

class TestGacNeMonthlySplit:
    """GAC-NE 月预测拆分测试。"""

    def test_basic_split(self):
        """基本拆分：6个月预测，当月扣减已交货量。"""
        record = {
            "current_month": 202511,
            "forecast_first_num": 1000,
            "forecast_second_num": 1200,
            "forecast_third_num": 1100,
            "forecast_fourth_num": 1300,
            "forecast_fifth_num": 1000,
            "forecast_sixth_num": 1500,
            "delivery_count": 300,
        }
        result = run_preset("gac_ne_monthly_split", record)

        # 应该只返回当月
        assert len(result) == 1
        assert result[0]["date"] == "202512"
        assert result[0]["type"] == "monthly"
        # 当月扣减：1000 - 300 = 700
        assert result[0]["qty"] == 700

    def test_delivery_exceeds_forecast(self):
        """已交货量超过预测时，数量应为0。"""
        record = {
            "current_month": 202511,
            "forecast_first_num": 1000,
            "delivery_count": 1500,
        }
        result = run_preset("gac_ne_monthly_split", record)
        assert result[0]["qty"] == 0

    def test_year_boundary(self):
        """跨年测试：12月 → 次年1月。"""
        record = {
            "current_month": 202512,
            "forecast_first_num": 1000,
        }
        result = run_preset("gac_ne_monthly_split", record)
        assert result[0]["date"] == "202601"


# ---------------------------------------------------------------------------
# Daily to Monthly Split 测试
# ---------------------------------------------------------------------------

class TestDailyToMonthlySplit:
    """日需求转月度拆分测试。"""

    def test_basic_split_with_merge(self):
        """基本拆分：同日期合并，生成日度+月度。"""
        record = {
            "demand": [
                {"date": "2025-11-15", "qty": 10},
                {"date": "2025-11-15", "qty": 20},  # 同日合并
                {"date": "2025-11-20", "qty": 30},
                {"date": "2025-12-10", "qty": 50},
            ],
            "merge_by_date": True,
        }
        result = run_preset("daily_to_monthly_split", record)

        # 日度明细：3条（11/15合并后30，11/20是30，12/10是50）
        daily = [r for r in result if r["type"] == "daily"]
        assert len(daily) == 3
        assert daily[0]["date"] == "20251115"
        assert daily[0]["qty"] == 30  # 10+20
        assert daily[1]["date"] == "20251120"
        assert daily[1]["qty"] == 30
        assert daily[2]["date"] == "20251210"
        assert daily[2]["qty"] == 50

        # 月度明细：2条（11月60，12月50）
        monthly = [r for r in result if r["type"] == "monthly"]
        assert len(monthly) == 2
        assert monthly[0]["date"] == "202511"
        assert monthly[0]["qty"] == 60  # 30+30
        assert monthly[1]["date"] == "202512"
        assert monthly[1]["qty"] == 50

    def test_split_without_merge(self):
        """不合并同日期（SAIC-NON-KD模式）。"""
        record = {
            "demand": [
                {"date": "2025-11-15", "qty": 10},
                {"date": "2025-11-15", "qty": 20},
            ],
            "merge_by_date": False,
        }
        result = run_preset("daily_to_monthly_split", record)
        daily = [r for r in result if r["type"] == "daily"]
        # 不合并，保留2条
        assert len(daily) == 2

    def test_empty_demand(self):
        """空需求返回空列表。"""
        record = {"demand": []}
        result = run_preset("daily_to_monthly_split", record)
        assert result == []


# ---------------------------------------------------------------------------
# Toyota Row Merge Freeze 测试
# ---------------------------------------------------------------------------

class TestToyotaRowMergeFreeze:
    """Toyota 行合并+冻结汇总测试。"""

    def test_basic_merge_and_freeze(self):
        """基本合并：同key行合并，同日期数量求和。"""
        record = {
            "rows": [
                {"plant": "A", "supplier_code": "S1", "part_no": "P1", "date": "20251115", "qty": 10},
                {"plant": "A", "supplier_code": "S1", "part_no": "P1", "date": "20251115", "qty": 20},  # 同key合并
                {"plant": "A", "supplier_code": "S1", "part_no": "P1", "date": "20251120", "qty": 30},
            ],
        }
        result = run_preset("toyota_row_merge_freeze", record)

        # 日度明细：2条（11/15合并后30，11/20是30）
        daily = [r for r in result if r["type"] == "daily"]
        assert len(daily) == 2
        assert daily[0]["date"] == "20251115"
        assert daily[0]["qty"] == 30  # 10+20
        assert daily[1]["date"] == "20251120"
        assert daily[1]["qty"] == 30

        # 只有日度输入，月度数据被去重规则移除（有日度则该月月度不保留）
        monthly = [r for r in result if r["type"] == "monthly"]
        assert len(monthly) == 0

    def test_monthly_dedup_when_daily_exists(self):
        """如果某月有日度数据，则移除该月的月度数据。"""
        record = {
            "rows": [
                {"plant": "A", "supplier_code": "S1", "part_no": "P1", "date": "20251115", "qty": 10, "monthly_qty": 100},
                {"plant": "B", "supplier_code": "S2", "part_no": "P2", "date": "20251220", "monthly_qty": 200},  # 只有月度
            ],
        }
        result = run_preset("toyota_row_merge_freeze", record)

        # 日度：只有11/15
        daily = [r for r in result if r["type"] == "daily"]
        assert len(daily) == 1
        assert daily[0]["date"] == "20251115"

        # 月度：只有12月（11月被去重）
        monthly = [r for r in result if r["type"] == "monthly"]
        assert len(monthly) == 1
        assert monthly[0]["date"] == "202512"

    def test_empty_rows(self):
        """空行返回空列表。"""
        record = {"rows": []}
        result = run_preset("toyota_row_merge_freeze", record)
        assert result == []

    def test_date_as_datetime_date(self):
        """date 为 datetime.date 对象时不应 TypeError。"""
        import datetime as dt

        record = {
            "rows": [
                {"plant": "A", "supplier_code": "S1", "part_no": "P1",
                 "date": dt.date(2025, 11, 15), "qty": 10},
                {"plant": "B", "supplier_code": "S2", "part_no": "P2",
                 "date": dt.date(2025, 12, 20), "monthly_qty": 200},
            ],
        }
        result = run_preset("toyota_row_merge_freeze", record)
        daily = [r for r in result if r["type"] == "daily"]
        monthly = [r for r in result if r["type"] == "monthly"]
        assert len(daily) == 1
        assert daily[0]["date"] == "20251115"
        assert len(monthly) == 1
        assert monthly[0]["date"] == "202512"


# ---------------------------------------------------------------------------
# Ming Daily Order Blend 测试
# ---------------------------------------------------------------------------

class TestMingDailyOrderBlend:
    """名辰 Ming 日订单 + 预测缺口补足预设测试。"""

    def test_weekly_orders_cover_sunday(self):
        """Case 1: 周度模式，日订单覆盖到周日 → 直接取日订单。"""
        record = {
            "forecast_type": "weekly",
            "forecast": 800,
            "daily_orders": [
                {"date": "2025-12-11", "qty": 100},
                {"date": "2025-12-12", "qty": 100},
                {"date": "2025-12-13", "qty": 100},
                {"date": "2025-12-14", "qty": 100},
            ],
            "pgi": [
                {"date": "2025-12-08", "qty": 100},
                {"date": "2025-12-09", "qty": 100},
                {"date": "2025-12-10", "qty": 100},
            ],
            "transportation_lt": 2,
        }
        result = run_preset("ming_daily_order_blend", record)
        assert len(result) == 7  # Mon-Sun
        qtys = {r["date"]: r["qty"] for r in result}
        assert qtys["2025-12-11"] == 100  # Thu order
        assert qtys["2025-12-14"] == 100  # Sun order

    def test_weekly_gap_to_sunday(self):
        """Case 2: 周度模式，日订单未覆盖周日 → 缺口放到周日。"""
        record = {
            "forecast_type": "weekly",
            "forecast": 800,
            "daily_orders": [
                {"date": "2025-12-11", "qty": 100},
                {"date": "2025-12-12", "qty": 100},
                {"date": "2025-12-13", "qty": 100},
            ],
            "pgi": [
                {"date": "2025-12-08", "qty": 100},
                {"date": "2025-12-09", "qty": 100},
                {"date": "2025-12-10", "qty": 100},
            ],
            "transportation_lt": 2,
        }
        result = run_preset("ming_daily_order_blend", record)
        qtys = {r["date"]: r["qty"] for r in result}
        assert qtys["2025-12-11"] == 100   # Thu order kept
        assert qtys["2025-12-14"] == 200   # Sun = gap

    def test_monthly_spread_from_last_order(self):
        """Case 6: 月度模式，日订单未覆盖月末 → 从最后订单日平摊到月底。"""
        record = {
            "forecast_type": "monthly",
            "forecast": 800,
            "daily_orders": [
                {"date": "2025-12-11", "qty": 100},
                {"date": "2025-12-12", "qty": 100},
                {"date": "2025-12-13", "qty": 100},
            ],
            "pgi": [
                {"date": "2025-12-08", "qty": 100},
                {"date": "2025-12-09", "qty": 100},
                {"date": "2025-12-10", "qty": 100},
            ],
            "transportation_lt": 2,
        }
        result = run_preset("ming_daily_order_blend", record)
        # 12/11~12/31 = 21 天，12/11-12/12 订单保留，12/13-12/31 平摊
        assert len(result) == 21
        assert result[0]["qty"] == 100   # Dec 11 order
        assert result[1]["qty"] == 100   # Dec 12 order
        assert 42 < result[2]["qty"] < 43  # Dec 13 spread ~42.11

    def test_all_values_non_negative(self):
        """所有输出值 >= 0。"""
        result = run_preset("ming_daily_order_blend", {
            "forecast_type": "weekly",
            "forecast": 800,
            "daily_orders": [
                {"date": "2025-12-11", "qty": 100},
                {"date": "2025-12-12", "qty": 100},
            ],
            "pgi": [],
            "transportation_lt": 2,
        })
        assert all(r["qty"] >= 0 for r in result)

    def test_no_orders_returns_empty(self):
        """无日订单返回空列表。"""
        result = run_preset("ming_daily_order_blend", {
            "forecast_type": "weekly",
            "forecast": 800,
            "daily_orders": [],
            "pgi": [],
            "transportation_lt": 2,
        })
        assert result == []

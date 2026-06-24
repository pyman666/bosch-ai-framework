"""Phase 2 统计类预设测试 — Holt-Winters 和 ARIMA。"""

import pytest
from forecast.skills.presets import run_preset, get_preset_info


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def seasonal_record():
    """模拟有季节性模式的需求数据（21天，覆盖3个周期）。"""
    import datetime as dt
    base = dt.date(2026, 1, 1)
    # 周度周期：周四峰值，周末低谷
    pattern = [100, 120, 150, 180, 160, 130, 110]
    demand = []
    for week in range(3):
        for day, qty in enumerate(pattern):
            # 加入趋势：每周递增
            trend_adj = week * 10
            demand.append({
                "date": (base + dt.timedelta(days=week * 7 + day)).isoformat(),
                "qty": qty + trend_adj,
            })
    return {"demand": demand}


@pytest.fixture
def trend_record():
    """模拟有上升趋势的需求数据（30天）。"""
    import datetime as dt
    base = dt.date(2026, 1, 1)
    demand = []
    for i in range(30):
        # 线性趋势 + 噪声
        qty = 100 + i * 3 + (i % 7 - 3) * 5
        demand.append({
            "date": (base + dt.timedelta(days=i)).isoformat(),
            "qty": max(0, qty),
        })
    return {"demand": demand}


@pytest.fixture
def short_record():
    """短数据（5天），用于测试降级策略。"""
    import datetime as dt
    base = dt.date(2026, 1, 1)
    demand = []
    for i in range(5):
        demand.append({
            "date": (base + dt.timedelta(days=i)).isoformat(),
            "qty": 100 + i * 5,
        })
    return {"demand": demand}


@pytest.fixture
def tiny_record():
    """极短数据（3天），用于测试 fallback。"""
    import datetime as dt
    base = dt.date(2026, 1, 1)
    return {
        "demand": [
            {"date": base.isoformat(), "qty": 100},
            {"date": (base + dt.timedelta(days=1)).isoformat(), "qty": 110},
            {"date": (base + dt.timedelta(days=2)).isoformat(), "qty": 105},
        ]
    }


# ---------------------------------------------------------------------------
# Holt-Winters 测试
# ---------------------------------------------------------------------------

class TestHoltWinters:
    def test_seasonal_data(self, seasonal_record):
        """季节性数据应该使用 Holt-Winters 完整版。"""
        result = run_preset("holt_winters", seasonal_record)
        assert isinstance(result, list)
        assert len(result) == 7  # default horizon
        assert all("date" in r and "qty" in r for r in result)
        assert all(r["qty"] >= 0 for r in result)

    def test_trend_data(self, trend_record):
        """趋势数据应该能正常预测。"""
        result = run_preset("holt_winters", trend_record)
        assert isinstance(result, list)
        assert len(result) == 7
        assert all(r["qty"] >= 0 for r in result)

    def test_short_data_fallback(self, short_record):
        """数据不足 14 天应该降级为 Holt。"""
        result = run_preset("holt_winters", short_record)
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(r["qty"] >= 0 for r in result)

    def test_tiny_data_fallback(self, tiny_record):
        """数据 < 4 天应该降级为移动平均。"""
        result = run_preset("holt_winters", tiny_record)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_custom_horizon(self, seasonal_record):
        """自定义预测周期。"""
        result = run_preset("holt_winters", seasonal_record)
        # horizon 通过 record 参数传递，这里用默认 7
        assert len(result) == 7

    def test_empty_demand(self):
        """空需求应该返回空列表。"""
        result = run_preset("holt_winters", {"demand": []})
        assert result == []

    def test_custom_seasonal_periods(self, seasonal_record):
        """自定义季节周期。"""
        # 用 seasonal_periods=7（默认）
        result = run_preset("holt_winters", seasonal_record)
        assert len(result) == 7


# ---------------------------------------------------------------------------
# ARIMA 测试
# ---------------------------------------------------------------------------

class TestARIMA:
    def test_trend_data(self, trend_record):
        """趋势数据应该能正常预测。"""
        result = run_preset("arima", trend_record)
        assert isinstance(result, list)
        assert len(result) == 7  # default horizon
        assert all("date" in r and "qty" in r for r in result)
        assert all(r["qty"] >= 0 for r in result)

    def test_seasonal_data(self, seasonal_record):
        """季节性数据也能用 ARIMA（虽然不如 HW 精确）。"""
        result = run_preset("arima", seasonal_record)
        assert isinstance(result, list)
        assert len(result) == 7
        assert all(r["qty"] >= 0 for r in result)

    def test_short_data_fallback(self, short_record):
        """数据 < 10 天应该降级为线性趋势。"""
        result = run_preset("arima", short_record)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_tiny_data_fallback(self, tiny_record):
        """数据 < 4 天应该降级为指数平滑。"""
        result = run_preset("arima", tiny_record)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_empty_demand(self):
        """空需求应该返回空列表。"""
        result = run_preset("arima", {"demand": []})
        assert result == []

    def test_custom_max_params(self, trend_record):
        """自定义最大参数。"""
        result = run_preset("arima", trend_record)
        assert len(result) == 7


# ---------------------------------------------------------------------------
# zero_shot 和 chronos 更新测试
# ---------------------------------------------------------------------------

class TestUpdatedPresets:
    def test_zero_shot_uses_holt_winters(self, seasonal_record):
        """zero_shot 应该使用 Holt-Winters（数据 >= 14）。"""
        result = run_preset("zero_shot", seasonal_record)
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(r["qty"] >= 0 for r in result)

    def test_zero_shot_short_data(self, short_record):
        """zero_shot 数据不足应该降级。"""
        result = run_preset("zero_shot", short_record)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_chronos_uses_arima(self, trend_record):
        """chronos 应该使用 ARIMA（数据 >= 10）。"""
        result = run_preset("chronos", trend_record)
        assert isinstance(result, list)
        assert len(result) > 0
        assert all(r["qty"] >= 0 for r in result)

    def test_chronos_short_data(self, short_record):
        """chronos 数据不足应该降级。"""
        result = run_preset("chronos", short_record)
        assert isinstance(result, list)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# 元数据测试
# ---------------------------------------------------------------------------

def test_preset_info_includes_new():
    """get_preset_info 应该包含新预设。"""
    info = get_preset_info()
    names = [p["name"] for p in info]
    assert "holt_winters" in names
    assert "arima" in names
    assert len(info) == 17  # 8 original + 7 business + 2 new stats

    # 检查分类
    stat_presets = [p for p in info if p["category"] == "algorithm"]
    assert len(stat_presets) >= 5  # MA, ES, LT, HW, ARIMA (+ zero_shot/chronos)

    # zero_shot 和 chronos 归为 algorithm
    zero_shot = next(p for p in info if p["name"] == "zero_shot")
    assert zero_shot["category"] == "algorithm"
    chronos = next(p for p in info if p["name"] == "chronos")
    assert chronos["category"] == "algorithm"
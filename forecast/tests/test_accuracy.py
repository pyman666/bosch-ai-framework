"""预测准确度评估测试 — 覆盖 compute_accuracy 的各种场景。"""

import pytest
from datetime import date

from forecast.core.accuracy import compute_accuracy
from forecast.models.forecast import TimeSeriesPoint


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def perfect_forecast():
    """完美预测：预测值 = 实际值"""
    dates = [date(2026, 1, i) for i in range(1, 8)]
    qtys = [100.0, 120.0, 110.0, 130.0, 125.0, 140.0, 135.0]
    forecast = [TimeSeriesPoint(date=d, qty=q) for d, q in zip(dates, qtys)]
    actual = [TimeSeriesPoint(date=d, qty=q) for d, q in zip(dates, qtys)]
    return forecast, actual


@pytest.fixture
def biased_forecast():
    """系统性高估：预测值 = 实际值 + 10"""
    dates = [date(2026, 1, i) for i in range(1, 8)]
    actual_qtys = [100.0, 120.0, 110.0, 130.0, 125.0, 140.0, 135.0]
    forecast_qtys = [q + 10.0 for q in actual_qtys]
    forecast = [TimeSeriesPoint(date=d, qty=q) for d, q in zip(dates, forecast_qtys)]
    actual = [TimeSeriesPoint(date=d, qty=q) for d, q in zip(dates, actual_qtys)]
    return forecast, actual


@pytest.fixture
def partial_overlap_forecast():
    """部分日期重叠"""
    forecast = [
        TimeSeriesPoint(date=date(2026, 1, 1), qty=100.0),
        TimeSeriesPoint(date=date(2026, 1, 2), qty=120.0),
        TimeSeriesPoint(date=date(2026, 1, 3), qty=110.0),
        TimeSeriesPoint(date=date(2026, 1, 4), qty=999.0),  # 不在 actual 中
    ]
    actual = [
        TimeSeriesPoint(date=date(2026, 1, 1), qty=100.0),
        TimeSeriesPoint(date=date(2026, 1, 2), qty=120.0),
        TimeSeriesPoint(date=date(2026, 1, 3), qty=110.0),
        TimeSeriesPoint(date=date(2026, 1, 5), qty=888.0),  # 不在 forecast 中
    ]
    return forecast, actual


@pytest.fixture
def no_overlap_forecast():
    """无重叠日期"""
    forecast = [TimeSeriesPoint(date=date(2026, 1, 1), qty=100.0)]
    actual = [TimeSeriesPoint(date=date(2026, 1, 2), qty=100.0)]
    return forecast, actual


@pytest.fixture
def zero_actual_forecast():
    """实际值为 0（测试 MAPE 除零处理）"""
    dates = [date(2026, 1, i) for i in range(1, 4)]
    forecast = [TimeSeriesPoint(date=d, qty=10.0) for d in dates]
    actual = [TimeSeriesPoint(date=d, qty=0.0) for d in dates]
    return forecast, actual


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestComputeAccuracy:
    """准确度计算测试。"""

    def test_perfect_forecast(self, perfect_forecast):
        """完美预测：所有误差为 0"""
        forecast, actual = perfect_forecast
        metrics = compute_accuracy(forecast, actual)

        assert metrics.data_points == 7
        assert metrics.mae == 0.0
        assert metrics.mape == 0.0
        assert metrics.rmse == 0.0
        assert metrics.smape == 0.0

    def test_biased_forecast(self, biased_forecast):
        """系统性高估：MAE = 10, RMSE = 10"""
        forecast, actual = biased_forecast
        metrics = compute_accuracy(forecast, actual)

        assert metrics.data_points == 7
        assert metrics.mae == 10.0
        assert metrics.rmse == 10.0
        # MAPE: 10/100, 10/120, 10/110, 10/130, 10/125, 10/140, 10/135
        # ≈ 0.1, 0.0833, 0.0909, 0.0769, 0.08, 0.0714, 0.0741 → 平均 ≈ 8.24%
        assert 8.0 < metrics.mape < 8.5
        # sMAPE: 10/210, 10/230, 10/220, 10/240, 10/235, 10/250, 10/245
        # avg(10/x) ≈ 0.0432 → 0.0432 * 200 ≈ 8.6...
        # 实际: sum(|e|/(|f|+|a|))/n * 200 ≈ 7.91%
        assert 7.5 < metrics.smape < 8.5

    def test_partial_overlap(self, partial_overlap_forecast):
        """部分重叠：仅计算共同日期"""
        forecast, actual = partial_overlap_forecast
        metrics = compute_accuracy(forecast, actual)

        assert metrics.data_points == 3
        assert metrics.mae == 0.0
        assert metrics.mape == 0.0
        assert metrics.rmse == 0.0
        assert metrics.smape == 0.0

    def test_no_overlap(self, no_overlap_forecast):
        """无重叠：返回 None"""
        forecast, actual = no_overlap_forecast
        metrics = compute_accuracy(forecast, actual)

        assert metrics.data_points == 0
        assert metrics.mae is None
        assert metrics.mape is None
        assert metrics.rmse is None
        assert metrics.smape is None

    def test_zero_actual(self, zero_actual_forecast):
        """实际值为 0：MAPE 跳过这些点，返回 None"""
        forecast, actual = zero_actual_forecast
        metrics = compute_accuracy(forecast, actual)

        assert metrics.data_points == 3
        assert metrics.mae == 10.0
        assert metrics.rmse == 10.0
        assert metrics.mape is None  # 所有实际值为 0，MAPE 无定义
        # sMAPE: 10/10 = 1.0 → 平均 1.0 * 200 = 200%
        assert metrics.smape == 200.0

    def test_single_point(self):
        """单个数据点"""
        forecast = [TimeSeriesPoint(date=date(2026, 1, 1), qty=110.0)]
        actual = [TimeSeriesPoint(date=date(2026, 1, 1), qty=100.0)]
        metrics = compute_accuracy(forecast, actual)

        assert metrics.data_points == 1
        assert metrics.mae == 10.0
        assert metrics.mape == 10.0  # 10/100 * 100
        assert metrics.rmse == 10.0
        # sMAPE: 10/210 * 200 ≈ 9.52%
        assert 9.5 < metrics.smape < 9.6

    def test_unordered_dates(self):
        """乱序日期：应正确对齐"""
        forecast = [
            TimeSeriesPoint(date=date(2026, 1, 3), qty=110.0),
            TimeSeriesPoint(date=date(2026, 1, 1), qty=100.0),
            TimeSeriesPoint(date=date(2026, 1, 2), qty=120.0),
        ]
        actual = [
            TimeSeriesPoint(date=date(2026, 1, 2), qty=120.0),
            TimeSeriesPoint(date=date(2026, 1, 3), qty=110.0),
            TimeSeriesPoint(date=date(2026, 1, 1), qty=100.0),
        ]
        metrics = compute_accuracy(forecast, actual)

        assert metrics.data_points == 3
        assert metrics.mae == 0.0

    def test_both_zero(self):
        """预测和实际都为 0：sMAPE = 0"""
        forecast = [TimeSeriesPoint(date=date(2026, 1, 1), qty=0.0)]
        actual = [TimeSeriesPoint(date=date(2026, 1, 1), qty=0.0)]
        metrics = compute_accuracy(forecast, actual)

        assert metrics.data_points == 1
        assert metrics.mae == 0.0
        assert metrics.mape is None  # 实际值为 0
        assert metrics.rmse == 0.0
        assert metrics.smape == 0.0  # 分子分母都为 0 → 0

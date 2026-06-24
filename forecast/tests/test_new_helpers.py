"""测试新添加的辅助函数 _group_days_by_month 和 _running_balance。"""

import datetime as dt
from forecast.skills.presets._business import _group_days_by_month, _running_balance


def test_group_days_by_month():
    """测试按月分组功能。"""
    days = [
        {"date": dt.date(2026, 1, 15), "qty": 100},
        {"date": dt.date(2026, 1, 20), "qty": 200},
        {"date": dt.date(2026, 2, 5), "qty": 300},
        {"date": dt.date(2026, 2, 10), "qty": 400},
    ]

    result = _group_days_by_month(days)

    assert len(result) == 2
    assert (2026, 1) in result
    assert (2026, 2) in result
    assert len(result[(2026, 1)]) == 2
    assert len(result[(2026, 2)]) == 2
    assert result[(2026, 1)][0]["qty"] == 100
    assert result[(2026, 2)][1]["qty"] == 400


def test_running_balance():
    """测试运行余额计算。"""
    demand = [
        {"date": dt.date(2026, 1, 1), "qty": 50},
        {"date": dt.date(2026, 1, 2), "qty": 30},
        {"date": dt.date(2026, 1, 3), "qty": 40},
    ]
    supply = {
        dt.date(2026, 1, 1): 10,
        dt.date(2026, 1, 2): 20,
        dt.date(2026, 1, 3): 0,
    }
    begin_inv = 100

    result = _running_balance(demand, supply, begin_inv)

    # Day 1: 100 + 10 - 50 = 60, net = 0
    # Day 2: 60 + 20 - 30 = 50, net = 0
    # Day 3: 50 + 0 - 40 = 10, net = 0
    assert len(result) == 3
    assert result[0]["balance"] == 60.0
    assert result[0]["qty"] == 0.0
    assert result[1]["balance"] == 50.0
    assert result[1]["qty"] == 0.0
    assert result[2]["balance"] == 10.0
    assert result[2]["qty"] == 0.0


def test_running_balance_with_shortage():
    """测试有短缺情况的运行余额。"""
    demand = [
        {"date": dt.date(2026, 1, 1), "qty": 150},  # 超出期初库存
        {"date": dt.date(2026, 1, 2), "qty": 30},
    ]
    supply = {
        dt.date(2026, 1, 1): 10,
        dt.date(2026, 1, 2): 20,
    }
    begin_inv = 100

    result = _running_balance(demand, supply, begin_inv)

    # Day 1: 100 + 10 - 150 = -40 (真实余额)，net = 40，余额 clamp 到 0 供下一天
    # Day 2: 0 + 20 - 30 = -10 (真实余额)，net = 10，余额 clamp 到 0 供下一天
    assert result[0]["balance"] == -40.0
    assert result[0]["qty"] == 40.0
    assert result[1]["balance"] == -10.0
    assert result[1]["qty"] == 10.0

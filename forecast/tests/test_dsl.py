"""DSL 引擎测试 — 覆盖所有内置函数和边界情况。"""

import pytest
from forecast.skills.dsl import eval_dsl, get_available_functions, tokenize


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_record():
    """基础测试数据。"""
    return {
        "demand": [
            {"date": "2026-01-01", "qty": 100},
            {"date": "2026-01-02", "qty": 120},
            {"date": "2026-01-03", "qty": 110},
            {"date": "2026-01-04", "qty": 130},
            {"date": "2026-01-05", "qty": 125},
        ],
        "pgi": [
            {"date": "2026-01-01", "qty": 50},
            {"date": "2026-01-02", "qty": 60},
        ],
        "beginningInventory": 500,
    }


# ---------------------------------------------------------------------------
# Tokenizer 测试
# ---------------------------------------------------------------------------

def test_tokenize_numbers():
    assert tokenize("123") == [{"kind": "number", "value": "123"}]
    assert tokenize("3.14") == [{"kind": "number", "value": "3.14"}]


def test_tokenize_names():
    assert tokenize("demand") == [{"kind": "name", "value": "demand"}]


def test_tokenize_function_call():
    tokens = tokenize("mean(demand)")
    kinds = [t["kind"] for t in tokens]
    assert "name" in kinds
    assert "op" in kinds


def test_tokenize_strings():
    assert tokenize("'hello'") == [{"kind": "string", "value": "hello"}]
    assert tokenize('"world"') == [{"kind": "string", "value": "world"}]


# ---------------------------------------------------------------------------
# 统计函数测试
# ---------------------------------------------------------------------------

def test_moving_average(sample_record):
    result = eval_dsl("moving_average(demand, 3)", sample_record)
    assert isinstance(result, list)
    assert len(result) == 5


def test_exponential_smoothing(sample_record):
    result = eval_dsl("exponential_smoothing(demand, 0.3)", sample_record)
    assert isinstance(result, list)
    assert len(result) == 5


def test_linear_trend(sample_record):
    result = eval_dsl("linear_trend(demand, 5)", sample_record)
    assert isinstance(result, list)
    # linear_trend 返回原序列 + 外推序列
    assert len(result) >= 5


def test_seasonal_index(sample_record):
    result = eval_dsl("seasonal_index(demand, 5)", sample_record)
    assert isinstance(result, list)
    assert len(result) == 5


# ---------------------------------------------------------------------------
# 算术聚合函数测试
# ---------------------------------------------------------------------------

def test_sum(sample_record):
    result = eval_dsl("sum(demand)", sample_record)
    assert result == 100 + 120 + 110 + 130 + 125


def test_mean(sample_record):
    result = eval_dsl("mean(demand)", sample_record)
    assert result == (100 + 120 + 110 + 130 + 125) / 5


def test_std(sample_record):
    result = eval_dsl("std(demand)", sample_record)
    assert isinstance(result, float)
    assert result > 0


def test_min(sample_record):
    result = eval_dsl("min(demand)", sample_record)
    assert result == 100


def test_max(sample_record):
    result = eval_dsl("max(demand)", sample_record)
    assert result == 130


def test_shift(sample_record):
    result = eval_dsl("shift(demand, 1)", sample_record)
    assert isinstance(result, list)
    assert result[0] == 0.0


def test_cumsum(sample_record):
    result = eval_dsl("cumsum(demand)", sample_record)
    assert isinstance(result, list)
    assert result[-1] == 100 + 120 + 110 + 130 + 125


# ---------------------------------------------------------------------------
# 供应链函数测试
# ---------------------------------------------------------------------------

def test_safety_stock(sample_record):
    result = eval_dsl("safety_stock(demand, 1.65)", sample_record)
    assert isinstance(result, float)
    assert result > 0


def test_inventory_planning(sample_record):
    result = eval_dsl("inventory_planning(moving_average(demand, 3), beginningInventory, pgi)", sample_record)
    assert isinstance(result, list)
    assert all(v >= 0 for v in result)


# ---------------------------------------------------------------------------
# 算术运算测试
# ---------------------------------------------------------------------------

def test_arithmetic(sample_record):
    result = eval_dsl("mean(demand) + 10", sample_record)
    expected = (100 + 120 + 110 + 130 + 125) / 5 + 10
    assert result == expected


def test_arithmetic_complex(sample_record):
    result = eval_dsl("(mean(demand) + 5) * 2", sample_record)
    expected = ((100 + 120 + 110 + 130 + 125) / 5 + 5) * 2
    assert result == expected


def test_division_by_zero(sample_record):
    result = eval_dsl("100 / 0", sample_record)
    assert result == 0.0


# ---------------------------------------------------------------------------
# 条件判断测试
# ---------------------------------------------------------------------------

def test_if_then_else(sample_record):
    result = eval_dsl("if_then_else(mean(demand) > 100, 10, 0)", sample_record)
    assert result == 10


def test_if_then_else_false(sample_record):
    result = eval_dsl("if_then_else(mean(demand) > 1000, 10, 0)", sample_record)
    assert result == 0


# ---------------------------------------------------------------------------
# 业务逻辑函数测试
# ---------------------------------------------------------------------------

def test_jitcall_priority_dsl():
    record = {
        "weekly_demand": 800,
        "demand": [
            {"date": "2026-01-05", "qty": 100},
            {"date": "2026-01-06", "qty": 100},
            {"date": "2026-01-07", "qty": 100},
        ],
        "jitcall": [
            {"date": "2026-01-06", "qty": 80},
        ],
        "pgi": [{"date": "2026-01-05", "qty": 50}],
    }
    result = eval_dsl("jitcall_priority(800, demand, jitcall, pgi, 3)", record)
    assert isinstance(result, list)
    assert len(result) == 3


def test_monthly_daily_blend_dsl():
    record = {
        "monthly_forecast": 3000,
        "demand": [
            {"date": "2026-01-01", "qty": 100},
            {"date": "2026-01-02", "qty": 200},
        ],
        "beginningInventory": 500,
        "ins": [],
        "pgi": [],
    }
    result = eval_dsl("monthly_daily_blend(demand, 3000, beginningInventory, ins)", record)
    assert isinstance(result, list)
    assert len(result) == 2


def test_balance_dsl(sample_record):
    result = eval_dsl("balance(beginningInventory, demand, pgi)", sample_record)
    assert isinstance(result, list)
    assert len(result) == 5


def test_net_demand_dsl():
    """测试 net_demand DSL 函数。

    注意：DSL 不支持列表字面量，所以通过 balance 函数的输出作为输入。
    """
    record = {
        "beginningInventory": 100,
        "demand": [
            {"date": "2026-01-01", "qty": 50},
            {"date": "2026-01-02", "qty": 60},
            {"date": "2026-01-03", "qty": 200},
        ],
        "pgi": [],
    }
    # 先算 balance，再算 net_demand
    bal = eval_dsl("balance(beginningInventory, demand, pgi)", record)
    assert isinstance(bal, list)
    # balance: 100-50=50, 50-60=-10, -10-200=-210
    # net_demand: 0, 10, 210
    net = eval_dsl("net_demand(balance(beginningInventory, demand, pgi))", record)
    assert isinstance(net, list)
    assert net[0] == 0    # balance=50 > 0
    assert net[1] == 10   # balance=-10 < 0
    assert net[2] == 210  # balance=-210 < 0


# ---------------------------------------------------------------------------
# 边界情况测试
# ---------------------------------------------------------------------------

def test_empty_demand():
    result = eval_dsl("sum(demand)", {"demand": []})
    assert result == 0


def test_single_value():
    result = eval_dsl("mean(demand)", {"demand": [{"date": "2026-01-01", "qty": 42}]})
    assert result == 42.0


def test_unknown_function():
    with pytest.raises(ValueError, match="Unknown function"):
        eval_dsl("nonexistent(demand)", {"demand": []})


def test_unknown_variable():
    with pytest.raises(ValueError, match="Unknown variable"):
        eval_dsl("unknown_field + 1", {"demand": []})


# ---------------------------------------------------------------------------
# 函数注册测试
# ---------------------------------------------------------------------------

def test_get_available_functions():
    funcs = get_available_functions()
    assert isinstance(funcs, list)
    assert len(funcs) >= 20  # 16 原有 + 4 新增

    names = [f["name"] for f in funcs]
    assert "jitcall_priority" in names
    assert "monthly_daily_blend" in names
    assert "balance" in names
    assert "net_demand" in names

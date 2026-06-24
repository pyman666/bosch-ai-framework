"""预设算法测试 — 覆盖所有预设的冒烟测试。"""

import pytest
from forecast.skills.presets import run_preset, get_preset_info


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_record():
    """基础测试数据。"""
    return {
        "carModel": "A3油车汇总",
        "color": "其他颜色",
        "demand": [
            {"date": "2026-01-01", "qty": 100},
            {"date": "2026-01-02", "qty": 120},
            {"date": "2026-01-03", "qty": 110},
            {"date": "2026-01-04", "qty": 130},
            {"date": "2026-01-05", "qty": 125},
            {"date": "2026-01-06", "qty": 140},
            {"date": "2026-01-07", "qty": 135},
        ],
        "pgi": [
            {"date": "2026-01-01", "qty": 50},
        ],
        "beginningInventory": 500,
    }


@pytest.fixture
def jitcall_record():
    """JITCall 优先级测试数据（模式 A）。"""
    return {
        "weekly_demand": 800,
        "demand": [
            {"date": "2026-01-05", "qty": 100},  # 周一
            {"date": "2026-01-06", "qty": 100},  # 周二
            {"date": "2026-01-07", "qty": 100},  # 周三
            {"date": "2026-01-08", "qty": 100},  # 周四
            {"date": "2026-01-09", "qty": 100},  # 周五
            {"date": "2026-01-10", "qty": 100},  # 周六
            {"date": "2026-01-11", "qty": 100},  # 周日
        ],
        "jitcall": [
            {"date": "2026-01-06", "qty": 80},   # 周二 JITCall
            {"date": "2026-01-07", "qty": 80},   # 周三 JITCall
            {"date": "2026-01-08", "qty": 80},   # 周四 JITCall
        ],
        "pgi": [
            {"date": "2026-01-05", "qty": 100},  # 周一 PGI
        ],
        "transportationLT": 3,
    }


@pytest.fixture
def monthly_daily_record():
    """月预测+日需求整合测试数据（模式 B）。"""
    return {
        "monthly_forecast": 8014,
        "demand": [
            {"date": "2026-01-01", "qty": 408},
            {"date": "2026-01-02", "qty": 227},
            {"date": "2026-01-03", "qty": 90},
            {"date": "2026-01-04", "qty": 0},
            {"date": "2026-01-05", "qty": 0},
        ],
        "beginningInventory": 3316,
        "ins": [
            {"date": "2026-01-03", "qty": 576},
        ],
        "pgi": [],
    }


# ---------------------------------------------------------------------------
# 统计类预设测试
# ---------------------------------------------------------------------------

def test_moving_average(sample_record):
    result = run_preset("moving_average", sample_record)
    assert isinstance(result, list)
    assert len(result) > 0
    assert all("date" in r and "qty" in r for r in result)
    # 移动平均应该在 100-140 之间
    assert all(90 <= r["qty"] <= 150 for r in result)


def test_exponential_smoothing(sample_record):
    result = run_preset("exponential_smoothing", sample_record)
    assert isinstance(result, list)
    assert len(result) > 0
    assert all("date" in r and "qty" in r for r in result)


def test_linear_trend(sample_record):
    result = run_preset("linear_trend", sample_record)
    assert isinstance(result, list)
    assert len(result) > 0
    assert all("date" in r and "qty" in r for r in result)


# ---------------------------------------------------------------------------
# 供应链类预设测试
# ---------------------------------------------------------------------------

def test_safety_stock_planning(sample_record):
    result = run_preset("safety_stock_planning", sample_record)
    assert isinstance(result, list)
    assert len(result) > 0
    assert all("date" in r and "qty" in r for r in result)


def test_inventory_optimization(sample_record):
    result = run_preset("inventory_optimization", sample_record)
    assert isinstance(result, list)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# 基础模型占位测试
# ---------------------------------------------------------------------------

def test_zero_shot(sample_record):
    result = run_preset("zero_shot", sample_record)
    assert isinstance(result, list)
    assert len(result) > 0


def test_timesfm(sample_record):
    result = run_preset("timesfm", sample_record)
    assert isinstance(result, list)
    assert len(result) > 0


def test_chronos(sample_record):
    result = run_preset("chronos", sample_record)
    assert isinstance(result, list)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# 业务逻辑预设测试
# ---------------------------------------------------------------------------

def test_jitcall_priority_basic(jitcall_record):
    """测试 JITCall 优先级基本逻辑。"""
    result = run_preset("jitcall_priority", jitcall_record)

    assert isinstance(result, list)
    assert len(result) == 7  # 7 天

    # 检查输出格式
    assert all("date" in r and "qty" in r for r in result)

    # 周二、周三、周四应该取 JITCall 值 (80)
    # 周一、周五、周六、周日 = 日需求(100) + 余量平摊(60/4=15) = 115
    # 余量 = 800 - 100(PGI) - 640(已分配) = 60
    qtys = {r["date"]: r["qty"] for r in result}

    assert qtys["2026-01-06"] == 80   # 周二 JITCall
    assert qtys["2026-01-07"] == 80   # 周三 JITCall
    assert qtys["2026-01-08"] == 80   # 周四 JITCall
    assert qtys["2026-01-05"] == 115  # 周一: 100 + 15
    assert qtys["2026-01-09"] == 115  # 周五: 100 + 15


def test_jitcall_priority_with_spread(jitcall_record):
    """测试余量平摊逻辑。"""
    # 修改周需求为 1000，使余量 > 0
    jitcall_record["weekly_demand"] = 1000

    result = run_preset("jitcall_priority", jitcall_record)

    # 总发货量应该接近周需求 - PGI
    total_qty = sum(r["qty"] for r in result)
    expected_total = 1000 - 100  # 周需求 - PGI

    # 允许一定误差（因为取整）
    assert abs(total_qty - expected_total) <= 10


def test_jitcall_priority_no_jitcall(jitcall_record):
    """测试没有 JITCall 的情况。"""
    jitcall_record["jitcall"] = []

    result = run_preset("jitcall_priority", jitcall_record)

    assert isinstance(result, list)
    assert len(result) == 7
    # 没有 JITCall 时，所有天应该取日需求值
    assert all(r["qty"] == 100 for r in result)


def test_monthly_daily_blend_basic(monthly_daily_record):
    """测试月预测+日需求整合基本逻辑。"""
    result = run_preset("monthly_daily_blend", monthly_daily_record)

    assert isinstance(result, list)
    assert len(result) == 31  # 1 月全月 31 天，缺失日由 daily_avg 补齐

    # 检查输出格式
    assert all("date" in r and "qty" in r for r in result)

    # 前 5 天有明确日需求（包括 qty=0），其余天由月预测日均补齐
    # 有需求的天取需求值，缺失天取 daily_avg = 8014/31 ≈ 258.52


def test_monthly_daily_blend_inventory_balance(monthly_daily_record):
    """测试库存 Balance 计算。"""
    result = run_preset("monthly_daily_blend", monthly_daily_record)

    # 期初库存 3316，前 3 天需求 408+227+90=725
    # 第 3 天有 INS 576
    # Balance 应该逐渐下降
    # 当 Balance < 0 时，日发货量 > 0

    # 前几天库存充足，日发货量应该为 0
    assert result[0]["qty"] == 0  # 3316 - 408 = 2908 > 0
    assert result[1]["qty"] == 0  # 2908 - 227 = 2681 > 0


def test_monthly_daily_blend_no_daily_demand(monthly_daily_record):
    """测试只有月预测，没有日需求的情况。"""
    monthly_daily_record["demand"] = []

    result = run_preset("monthly_daily_blend", monthly_daily_record)

    # 没有日需求时应该返回空列表
    assert result == []


# ---------------------------------------------------------------------------
# 元数据测试
# ---------------------------------------------------------------------------

def test_get_preset_info():
    """测试预设元数据。"""
    info = get_preset_info()

    assert isinstance(info, list)
    assert len(info) >= 10  # 至少 10 个预设

    # 检查每个预设都有必要字段
    for preset in info:
        assert "name" in preset
        assert "description" in preset
        assert "parameters" in preset
        assert "category" in preset

    # 检查新增的业务逻辑预设
    names = [p["name"] for p in info]
    assert "fwdy_jitcall_priority" in names
    assert "geely_monthly_daily_blend" in names

    # 检查业务逻辑预设的分类
    biz_presets = [p for p in info if p["category"] == "business"]
    assert len(biz_presets) == 7  # 6 original + ming_daily_order_blend


def test_unknown_preset():
    """测试未知预设名称。"""
    with pytest.raises(ValueError, match="Unknown preset"):
        run_preset("nonexistent_preset", {})


# ---------------------------------------------------------------------------
# 边界情况测试
# ---------------------------------------------------------------------------

def test_empty_demand():
    """测试空需求数据。"""
    record = {"demand": []}

    # 所有预设应该能处理空数据
    for preset in ["moving_average", "exponential_smoothing", "linear_trend",
                   "jitcall_priority", "monthly_daily_blend"]:
        result = run_preset(preset, record)
        assert result == [] or isinstance(result, list)


def test_single_day_demand():
    """测试单日需求数据。"""
    record = {
        "demand": [{"date": "2026-01-01", "qty": 100}],
        "pgi": [],
        "beginningInventory": 500,
    }

    result = run_preset("moving_average", record)
    assert len(result) > 0


def test_zero_values():
    """测试零值处理。"""
    record = {
        "demand": [
            {"date": "2026-01-01", "qty": 0},
            {"date": "2026-01-02", "qty": 0},
        ],
        "pgi": [],
        "beginningInventory": 0,
    }

    result = run_preset("moving_average", record)
    assert all(r["qty"] == 0 for r in result)

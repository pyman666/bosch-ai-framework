"""Python Skill sandbox and review security tests."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forecast.core.executor import SandboxError, execute_python_skill, validate_python_skill_code
from forecast.core.skill_manager import create_skill, review_skill
from forecast.database import Base
from forecast.models.skill import SkillCreate, SkillType


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_python_skill_allows_whitelisted_imports_and_math():
    code = """
import math
from statistics import mean


def forecast(record):
    return [math.sqrt(16), mean([1, 2, 3])]
"""

    validate_python_skill_code(code)
    assert execute_python_skill(code, {}) == [4.0, 2]


@pytest.mark.parametrize(
    "code, expected",
    [
        ("import os\n\ndef forecast(record):\n    return [1]", "Import of 'os'"),
        ("from subprocess import run\n\ndef forecast(record):\n    return [1]", "Import of 'subprocess'"),
        ("from math import *\n\ndef forecast(record):\n    return [sqrt(4)]", "Wildcard imports"),
        ("def forecast(record):\n    return [eval('1 + 1')]", "Call to 'eval'"),
        ("def forecast(record):\n    return [open('x.txt').read()]", "Call to 'open'"),
        ("def forecast(record):\n    return [record.__class__]", "dunder attribute"),
        ("def forecast(record):\n    return [__builtins__]", "dunder name"),
    ],
)
def test_python_skill_static_security_blocks_dangerous_code(code, expected):
    with pytest.raises(SandboxError, match=expected):
        validate_python_skill_code(code)


def test_execute_python_skill_runs_static_check_before_execution():
    code = """
def forecast(record):
    return [eval('1 + 1')]
"""

    with pytest.raises(SandboxError, match="eval"):
        execute_python_skill(code, {})


# ---------------------------------------------------------------------------
# Sandbox helper injection tests — 验证预注入的 helper 签名和可调用性
# ---------------------------------------------------------------------------

SANDBOX_HELPER_NAMES = [
    "date",
    "timedelta",
    "prep_demand",
    "build_date_map",
    "group_by_iso_week",
    "priority_fill",
    "spread_remainder",
    "merge_sum_by_key",
    "run_jitcall_priority",
    "run_monthly_daily_blend",
]


def test_sandbox_helpers_are_injected():
    """验证 10 个 sandbox helper 符号均在 build_sandbox_helpers() 中注册。"""
    from forecast.core.executor import build_sandbox_helpers

    helpers = build_sandbox_helpers()
    for name in SANDBOX_HELPER_NAMES:
        assert name in helpers, f"Sandbox helper '{name}' not in build_sandbox_helpers()"


def test_prep_demand_returns_sorted_days():
    """prep_demand 应该解析、排序并返回 [{date, qty}]，无数据返回 []。"""
    from forecast.core.executor import prepare_python_skill
    from datetime import date

    code = """
def forecast(record):
    days = prep_demand(record)
    if not days:
        return []
    return [{"date": d["date"].isoformat(), "qty": d["qty"]} for d in days]
"""
    fn, _ = prepare_python_skill(code)

    # 有数据：应排序
    record = {
        "demand": [
            {"date": "2025-11-27", "qty": 300},
            {"date": "2025-11-25", "qty": 100},
        ],
    }
    result = fn(record)
    assert len(result) == 2
    assert result[0]["date"] == "2025-11-25"
    assert result[1]["date"] == "2025-11-27"

    # 无数据：返回空列表
    assert fn({"demand": []}) == []


def test_run_jitcall_priority_wrapper_extracts_weekly_demand():
    """run_jitcall_priority sandbox wrapper 应自动从 record['weekly_demand'] 提取 total_weekly。"""
    from forecast.core.executor import prepare_python_skill

    code = """
def forecast(record):
    return run_jitcall_priority(record, divide_by_weeks=False)
"""
    fn, _ = prepare_python_skill(code)

    record = {
        "weekly_demand": 700,
        "demand": [
            {"date": "2025-11-24", "qty": 100},
            {"date": "2025-11-25", "qty": 100},
        ],
        "jitcall": [],
        "pgi": [],
    }
    result = fn(record)
    assert len(result) == 2
    assert all("date" in r and "qty" in r for r in result)


def test_sandbox_has_date_and_timedelta():
    """date 和 timedelta 可在 sandbox 中直接使用，无需 import。"""
    from forecast.core.executor import prepare_python_skill

    code = """
def forecast(record):
    d = date(2025, 11, 25)
    td = timedelta(days=7)
    return [{"date": (d + td).isoformat(), "qty": 100}]
"""
    fn, _ = prepare_python_skill(code)
    result = fn({})
    assert result[0]["date"] == "2025-12-02"
    assert result[0]["qty"] == 100


def test_sandbox_globals_isolation():
    """验证不同请求间的模块级可变状态隔离。

    若用户 Python skill 写入全局 list/dict，
    不应污染后续调用（修复前 LRU 缓存会共享 globals）。
    """
    from forecast.core.executor import prepare_python_skill

    # Skill that appends to a module-level list on each call
    code = """
results_log = []

def forecast(record):
    results_log.append(record.get("x", 0))
    return [{"x": len(results_log)}]
"""
    fn1, globals1 = prepare_python_skill(code)
    fn2, globals2 = prepare_python_skill(code)

    # First call: log length = 1
    r1 = fn1({"x": 100})
    assert r1[0]["x"] == 1

    # Second call on same fn: log length = 2 (same globals, expected)
    r2 = fn1({"x": 200})
    assert r2[0]["x"] == 2

    # Call via a fresh prepare: should start from 1 again (isolated globals)
    r3 = fn2({"x": 300})
    assert r3[0]["x"] == 1, \
        f"globals not isolated — expected 1, got {r3[0]['x']}"


def test_review_python_skill_rejects_dangerous_code(db_session):
    skill = create_skill(
        db_session,
        SkillCreate(
            name="dangerous python",
            skill_type=SkillType.PYTHON,
            python_code="import os\n\ndef forecast(record):\n    return [1]",
        ),
    )

    with pytest.raises(ValueError, match="security check failed"):
        review_skill(db_session, skill.id)


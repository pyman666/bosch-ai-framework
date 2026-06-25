"""Skill 执行引擎 — 调度 DSL、Python 沙箱和预设 Skill 的执行.

通用沙箱能力已提取到 ``infra.agent.sandbox``，本模块只保留 forecast 领域逻辑:
- 沙箱 helper 注入 (forecast 业务函数)
- Skill 调度器 (DSL / Python / Preset)
- 结果模型转换
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from infra.agent.sandbox import (
    SandboxError,
    compile_code,
    execute_sandbox,
    prepare_sandbox,
    restricted_import,
    validate_code as validate_python_skill_code,
)
from forecast.models.forecast import ForecastInput, ForecastOutput, TimeSeriesPoint
from forecast.skills.dsl import eval_dsl
from forecast.skills.presets._helpers import (
    build_date_qty_map,
    merge_sum_by_key,
    prep_demand,
)
from forecast.skills.presets._business import (
    group_days_by_iso_week,
    _group_days_by_month,
    priority_fill,
    spread_remainder,
    _running_balance,
    run_jitcall_priority,
    geely_monthly_daily_blend,
    ming_daily_order_blend,
)

log = logging.getLogger(__name__)

# 向后兼容别名
compile_python_skill_code = compile_code


# ---------------------------------------------------------------------------
# 沙箱 helper 注入 (forecast 特有)
# ---------------------------------------------------------------------------

def sandbox_run_jitcall_priority(
    record: dict[str, Any], divide_by_weeks: bool = False
) -> list[dict[str, Any]]:
    """Sandbox adapter: auto-extracts total_weekly from record['weekly_demand'].

    The real run_jitcall_priority requires total_weekly as a keyword argument,
    but LLM-generated code expects the simplified (record, divide_by_weeks) signature.
    """
    return run_jitcall_priority(
        record,
        total_weekly=float(record.get("weekly_demand", 0) or 0),
        divide_by_weeks=divide_by_weeks,
    )


def build_sandbox_helpers() -> dict[str, Any]:
    """构建注入沙箱全局命名空间的 forecast helper 字典."""
    return {
        # datetime types
        "date": date,
        "timedelta": timedelta,
        # data layer
        "prep_demand": prep_demand,
        "build_date_map": build_date_qty_map,
        # grouping
        "group_by_iso_week": group_days_by_iso_week,
        "group_by_month": _group_days_by_month,
        # algorithm atoms
        "priority_fill": priority_fill,
        "spread_remainder": spread_remainder,
        "merge_sum_by_key": merge_sum_by_key,
        "running_balance": _running_balance,
        # full pipelines
        "run_jitcall_priority": sandbox_run_jitcall_priority,
        "run_monthly_daily_blend": geely_monthly_daily_blend,
        "run_ming_daily_order_blend": ming_daily_order_blend,
    }


# ---------------------------------------------------------------------------
# Python skill (thin wrapper over infra sandbox)
# ---------------------------------------------------------------------------

def prepare_python_skill(code: str) -> tuple[Any, dict]:
    """编译并加载 Python Skill，返回 (forecast_fn, sandbox_globals).

    编译结果通过 LRU 缓存复用，但每次调用都会重建 sandbox_globals 并重新 exec，
    避免用户 Python skill 写入模块级可变状态导致不同请求间状态污染。
    """
    return prepare_sandbox(
        code,
        helpers=build_sandbox_helpers(),
        required_fn="forecast",
    )


def execute_python_skill(code: str, record: dict[str, Any]) -> Any:
    """在受限沙箱中执行 Python Skill 脚本."""
    return execute_sandbox(
        code,
        record,
        helpers=build_sandbox_helpers(),
        required_fn="forecast",
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Skill dispatcher
# ---------------------------------------------------------------------------

def execute_skill(
    skill_type: str,
    dsl_expression: str | None,
    python_code: str | None,
    preset_name: str | None,
    input_data: ForecastInput,
) -> ForecastOutput:
    """对单条输入记录执行预测 Skill.

    Args:
        skill_type: "dsl" | "python" | "preset"
        dsl_expression: DSL 表达式字符串
        python_code: Python 源代码
        preset_name: 预设方法名称
        input_data: 单条预测输入记录

    Returns:
        包含预测时间序列的 ForecastOutput.
    """
    record = input_data.model_dump(by_alias=True)

    if skill_type == "dsl" and dsl_expression:
        result = eval_dsl(dsl_expression, record)
        return _dsl_result_to_output(input_data, result)

    elif skill_type == "python" and python_code:
        result = execute_python_skill(python_code, record)
        return _python_result_to_output(input_data, result)

    elif skill_type == "preset" and preset_name:
        from forecast.skills.presets import run_preset
        result = run_preset(preset_name, record)
        return _python_result_to_output(input_data, result)

    else:
        raise ValueError(
            f"Invalid skill config: type={skill_type}, has_dsl={bool(dsl_expression)}, "
            f"has_python={bool(python_code)}, preset={preset_name}"
        )


# ---------------------------------------------------------------------------
# Result conversion
# ---------------------------------------------------------------------------

def _floats_to_forecast(
    inp: ForecastInput, values: list[float]
) -> list[TimeSeriesPoint]:
    """将 float 列表转换为 TimeSeriesPoint 列表."""
    last_date = inp.demand[-1].date if inp.demand else date.today()
    return [
        TimeSeriesPoint(date=last_date + timedelta(days=i + 1), qty=float(v))
        for i, v in enumerate(values)
    ]


def _dsl_result_to_output(inp: ForecastInput, result: Any) -> ForecastOutput:
    """将 DSL 结果（float 或 list[float]）转换为 ForecastOutput."""
    if isinstance(result, (int, float)):
        forecast = _floats_to_forecast(inp, [result])
    elif isinstance(result, list):
        forecast = _floats_to_forecast(inp, result)
    else:
        forecast = []

    return ForecastOutput(
        **inp.extra_for_output,
        forecast=forecast,
        metadata={"method": "dsl"},
    )


def _python_result_to_output(inp: ForecastInput, result: Any) -> ForecastOutput:
    """将 Python/预设 Skill 的结果转换为 ForecastOutput."""
    if isinstance(result, list):
        if result and isinstance(result[0], dict):
            forecast = [
                TimeSeriesPoint(
                    date=d.get("date", date.today()),
                    qty=float(d.get("qty", d.get("value", 0))),
                )
                for d in result
            ]
        elif result and isinstance(result[0], (int, float)):
            forecast = _floats_to_forecast(inp, result)
        else:
            forecast = []
    elif isinstance(result, (int, float)):
        forecast = _floats_to_forecast(inp, [result])
    else:
        forecast = []

    return ForecastOutput(
        **inp.extra_for_output,
        forecast=forecast,
        metadata={"method": "python_or_preset"},
    )

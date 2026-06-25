"""Skill 执行引擎 — 调度 DSL、Python 沙箱和预设 Skill 的执行。"""

from __future__ import annotations

import ast
import builtins
import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

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

# ---------------------------------------------------------------------------
# Python 沙箱 — AST 验证 + 受限 import + 线程池超时执行
# ---------------------------------------------------------------------------

ALLOWED_BUILTINS = {
    "abs", "all", "any", "bool", "dict", "enumerate", "float", "int",
    "len", "list", "map", "max", "min", "pow", "range", "round", "set",
    "sorted", "str", "sum", "tuple", "zip", "True", "False", "None",
    "print", "isinstance", "type",
}

ALLOWED_MODULES = {
    "math", "statistics", "numpy", "pandas", "scipy",
    "sklearn", "statsmodels",
}

BLOCKED_MODULES = {
    "os", "subprocess", "sys", "socket", "shutil", "importlib",
    "__builtins__", "builtins", "ctypes", "multiprocessing", "threading",
    "signal", "posix", "nt", "winreg", "msvcrt",
}

BLOCKED_CALL_NAMES = {
    "eval", "exec", "open", "compile", "input", "globals", "locals",
    "vars", "dir", "getattr", "setattr", "delattr", "__import__",
    "breakpoint", "help",
}

BLOCKED_NAME_PREFIXES = ("__",)

_skill_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="skill")


class SandboxError(Exception):
    """沙箱规则违反时抛出的异常。"""
    pass


def _assert_allowed_module(name: str) -> None:
    top_level = name.split(".")[0]
    if top_level in BLOCKED_MODULES:
        raise SandboxError(f"Import of '{name}' is not allowed in skill sandbox")
    if top_level not in ALLOWED_MODULES:
        raise SandboxError(
            f"Import of '{name}' is not in the allowed modules list: {sorted(ALLOWED_MODULES)}"
        )


def restricted_import(name: str, globals_dict: dict, locals_dict: dict, fromlist: tuple, level: int):
    """沙箱专用的 __import__ 钩子。"""
    _assert_allowed_module(name)
    return __import__(name, globals_dict, locals_dict, fromlist, level)


def validate_python_skill_code(code: str) -> None:
    """静态检查 Python Skill 代码，作为人工审核前的第一道安全门。"""
    try:
        tree = ast.parse(code, filename="<skill>", mode="exec")
    except SyntaxError as exc:
        raise SandboxError(f"Python skill syntax error: {exc.msg} at line {exc.lineno}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _assert_allowed_module(alias.name)

        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise SandboxError("Relative imports are not allowed in skill sandbox")
            if not node.module:
                raise SandboxError("Import without module is not allowed in skill sandbox")
            _assert_allowed_module(node.module)
            if any(alias.name == "*" for alias in node.names):
                raise SandboxError("Wildcard imports are not allowed in skill sandbox")

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BLOCKED_CALL_NAMES:
                raise SandboxError(f"Call to '{func.id}' is not allowed in skill sandbox")
            if isinstance(func, ast.Attribute) and func.attr.startswith(BLOCKED_NAME_PREFIXES):
                raise SandboxError(f"Access to dunder attribute '{func.attr}' is not allowed")

        elif isinstance(node, ast.Attribute):
            if node.attr.startswith(BLOCKED_NAME_PREFIXES):
                raise SandboxError(f"Access to dunder attribute '{node.attr}' is not allowed")

        elif isinstance(node, ast.Name):
            if node.id.startswith(BLOCKED_NAME_PREFIXES):
                raise SandboxError(f"Use of dunder name '{node.id}' is not allowed")


@lru_cache(maxsize=256)
def compile_python_skill_code(code: str) -> Any:
    """编译 Python Skill 代码对象（缓存层）。"""
    validate_python_skill_code(code)
    return compile(code, "<skill>", "exec")


# ---------------------------------------------------------------------------
# 沙箱 helper 注入 (forecast 特有)
# ---------------------------------------------------------------------------

def sandbox_run_jitcall_priority(record: dict[str, Any], divide_by_weeks: bool = False) -> list[dict[str, Any]]:
    """Sandbox adapter: auto-extracts total_weekly from record['weekly_demand']."""
    return run_jitcall_priority(
        record,
        total_weekly=float(record.get("weekly_demand", 0) or 0),
        divide_by_weeks=divide_by_weeks,
    )


def build_sandbox_helpers() -> dict[str, Any]:
    """构建注入沙箱全局命名空间的 helper 字典。"""
    return {
        "date": date,
        "timedelta": timedelta,
        "prep_demand": prep_demand,
        "build_date_map": build_date_qty_map,
        "group_by_iso_week": group_days_by_iso_week,
        "group_by_month": _group_days_by_month,
        "priority_fill": priority_fill,
        "spread_remainder": spread_remainder,
        "merge_sum_by_key": merge_sum_by_key,
        "running_balance": _running_balance,
        "run_jitcall_priority": sandbox_run_jitcall_priority,
        "run_monthly_daily_blend": geely_monthly_daily_blend,
        "run_ming_daily_order_blend": ming_daily_order_blend,
    }


def prepare_python_skill(code: str) -> tuple[Any, dict]:
    """编译并加载 Python Skill，返回 (forecast_fn, sandbox_globals) 供复用。"""
    compiled = compile_python_skill_code(code)

    sandbox_globals = {
        "__builtins__": {
            k: v for k, v in builtins.__dict__.items()
            if k in ALLOWED_BUILTINS
        },
        "__name__": "__fcst_skill__",
        **build_sandbox_helpers(),
    }
    sandbox_globals["__builtins__"]["__import__"] = restricted_import

    exec(compiled, sandbox_globals, sandbox_globals)

    if "forecast" not in sandbox_globals:
        raise SandboxError("Python skill must define a 'forecast(record) -> list[dict]' function")

    return sandbox_globals["forecast"], sandbox_globals


def execute_python_skill(code: str, record: dict[str, Any]) -> Any:
    """在受限沙箱中执行 Python Skill 脚本。"""
    forecast_fn, _ = prepare_python_skill(code)

    future = _skill_executor.submit(forecast_fn, record)
    try:
        result = future.result(timeout=30)
    except FuturesTimeoutError:
        future.cancel()
        raise SandboxError("Skill execution timeout: 30s exceeded")
    except SandboxError:
        raise
    except Exception as e:
        raise SandboxError(f"Skill execution failed: {e}")

    return result


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
    """对单条输入记录执行预测 Skill。"""
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
        raise ValueError(f"Invalid skill config: type={skill_type}, has_dsl={bool(dsl_expression)}, "
                         f"has_python={bool(python_code)}, preset={preset_name}")


# ---------------------------------------------------------------------------
# Result conversion
# ---------------------------------------------------------------------------

def _floats_to_forecast(inp: ForecastInput, values: list[float]) -> list[TimeSeriesPoint]:
    """将 float 列表转换为 TimeSeriesPoint 列表。"""
    last_date = inp.demand[-1].date if inp.demand else date.today()
    return [TimeSeriesPoint(date=last_date + timedelta(days=i + 1), qty=float(v)) for i, v in enumerate(values)]


def _dsl_result_to_output(inp: ForecastInput, result: Any) -> ForecastOutput:
    """将 DSL 结果（float 或 list[float]）转换为 ForecastOutput。"""
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
    """将 Python/预设 Skill 的结果转换为 ForecastOutput。"""
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

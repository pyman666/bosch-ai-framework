"""Agent 工具 — 基于注册表模式的函数调用 (function calling) 工具集合。"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from forecast.core.memory import search_memory

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------

@dataclass
class _Tool:
    name: str
    definition: dict[str, Any]
    handler: Callable[..., Any]
    is_async: bool = False


_registry: dict[str, _Tool] = {}


def _register(name: str, definition: dict[str, Any], is_async: bool = False):
    """装饰器：注册工具 handler 并绑定 schema 定义。"""

    def decorator(fn: Callable) -> Callable:
        _registry[name] = _Tool(name=name, definition=definition, handler=fn, is_async=is_async)
        return fn

    return decorator


# 对外暴露的 schema 列表，由注册表自动生成
def _build_tool_definitions() -> list[dict[str, Any]]:
    return [t.definition for t in _registry.values()]


# ---------------------------------------------------------------------------
# 模块级上下文引用 — 当前输入数据（每次 agent 循环前设置）
# ---------------------------------------------------------------------------

_current_inputs: contextvars.ContextVar[list[dict[str, Any]]] = contextvars.ContextVar(
    "_current_inputs", default=[]
)
_current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_session_id", default=""
)


def set_context(session_id: str, inputs: list[dict[str, Any]] | None):
    """设置工具调用的当前上下文。每次 Agent 轮次前调用。"""
    _current_session_id.set(session_id)
    _current_inputs.set(inputs or [])


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """按名称分发工具调用。返回 JSON 字符串结果。"""
    tool = _registry.get(name)
    if not tool:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        if tool.is_async:
            return await tool.handler(arguments)
        return tool.handler(arguments)
    except Exception as e:
        log.exception(f"Tool {name} failed: {e}")
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

_COMPUTE_FIELDS = {
    "demand", "pgi", "beginningInventory", "other_factors",
    "other_factors_to_be_added", "jitcall", "transportationLT",
    "weekly_demand",
    "monthly_forecast", "ins", "forecast_period",
}


def _identity(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if k not in _COMPUTE_FIELDS and not isinstance(v, (list, dict))}


def _suggest_params(qtys: list[float], analysis: dict) -> dict:
    """根据数据分析结果推荐预设参数。"""
    n = len(qtys)
    mean_q = analysis.get("demand_mean", 0)
    std_q = analysis.get("demand_std", 0)
    cv = std_q / mean_q if mean_q > 0 else 0

    params = {}

    # moving_average: window 选择
    if cv < 0.2:
        params["moving_average"] = {"window": min(14, n // 2)}
    elif cv < 0.5:
        params["moving_average"] = {"window": min(7, n // 2)}
    else:
        params["moving_average"] = {"window": min(3, max(1, n // 3))}

    # exponential_smoothing: alpha 选择
    if analysis.get("trend") in ("上升", "下降"):
        params["exponential_smoothing"] = {"alpha": 0.5}
    else:
        params["exponential_smoothing"] = {"alpha": 0.2}

    # holt_winters: seasonal_periods
    if analysis.get("seasonal_detected"):
        sp = analysis.get("seasonal_period", 7)
        params["holt_winters"] = {"seasonal_periods": sp, "horizon": min(14, n // 2)}
    else:
        params["holt_winters"] = {"seasonal_periods": 7, "horizon": 7}

    # linear_trend: window
    if analysis.get("trend_confidence") == "显著":
        params["linear_trend"] = {"window": min(n, 30)}
    else:
        params["linear_trend"] = {"window": min(14, n)}

    # safety_stock: z_score
    if cv > 0.5:
        params["safety_stock_planning"] = {"z_score": 1.96, "window": min(30, n)}
    else:
        params["safety_stock_planning"] = {"z_score": 1.65, "window": min(30, n)}

    return params


# ---------------------------------------------------------------------------
# Tool: preview_forecast_data
# ---------------------------------------------------------------------------

@_register("preview_forecast_data", {
    "type": "function",
    "function": {
        "name": "preview_forecast_data",
        "description": "查看输入数据的摘要和前 N 行，了解数据的结构和特征",
        "parameters": {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "integer",
                    "description": "展示前 N 行数据，默认 20",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
})
def _preview_data(args: dict) -> str:
    rows = args.get("rows", 20)
    inputs = _current_inputs.get()
    if not inputs:
        return json.dumps({"summary": "没有加载输入数据。请先上传 forecast.json 格式数据。"})

    summary = {
        "total_records": len(inputs),
        "identity_fields": sorted(set().union(*(_identity(r).keys() for r in inputs))),
        "fields": ["demand", "pgi", "beginningInventory", "other_factors"],
        "sample": [],
    }
    for rec in inputs[: min(rows, len(inputs))]:
        item = {
            **_identity(rec),
            "beginningInventory": rec.get("beginningInventory", 0),
            "demand_dates": f"{len(rec.get('demand', []))} points",
            "pgi_dates": f"{len(rec.get('pgi', []))} points",
            "demand_sample": rec.get("demand", [])[:5],
            "pgi_sample": rec.get("pgi", [])[:5],
        }
        summary["sample"].append(item)
    return json.dumps(summary, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Tool: list_preset_methods
# ---------------------------------------------------------------------------

@_register("list_preset_methods", {
    "type": "function",
    "function": {
        "name": "list_preset_methods",
        "description": "列出所有可用的内置预测方法（统计/ML 模型），帮助用户选择合适的算法",
        "parameters": {"type": "object", "properties": {}},
    },
})
def _list_presets(_args: dict) -> str:
    from forecast.skills.presets import get_preset_info

    return json.dumps(get_preset_info(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: analyze_data_pattern
# ---------------------------------------------------------------------------

@_register("analyze_data_pattern", {
    "type": "function",
    "function": {
        "name": "analyze_data_pattern",
        "description": "分析输入数据的统计特征：趋势、季节性、波动性、缺失值等，帮助设计预测公式",
        "parameters": {"type": "object", "properties": {}},
    },
})
def _analyze_data(_args: dict) -> str:
    inputs = _current_inputs.get()
    if not inputs:
        return json.dumps({"error": "没有加载输入数据。"})

    analyses = []
    for rec in inputs[:3]:  # analyze first 3 records
        demand = rec.get("demand", [])
        qtys = [float(d.get("qty", 0)) for d in demand] if demand else []

        analysis = {
            **_identity(rec),
            "beginningInventory": rec.get("beginningInventory", 0),
            "demand_points": len(qtys),
        }

        if qtys:
            n = len(qtys)
            mean_q = sum(qtys) / n
            std_q = (sum((q - mean_q) ** 2 for q in qtys) / n) ** 0.5
            analysis.update({
                "demand_mean": round(mean_q, 2),
                "demand_std": round(std_q, 2),
                "demand_cv": round(std_q / mean_q, 3) if mean_q > 0 else 0,
                "demand_min": min(qtys),
                "demand_max": max(qtys),
                "demand_total": sum(qtys),
                "zero_days": sum(1 for q in qtys if q == 0),
                "non_zero_days": sum(1 for q in qtys if q > 0),
            })

            # ---- Trend detection: Mann-Kendall 简化版 ----
            if n >= 4:
                s = 0
                for i in range(n):
                    for j in range(i + 1, n):
                        diff = qtys[j] - qtys[i]
                        s += (1 if diff > 0 else (-1 if diff < 0 else 0))
                var_s = n * (n - 1) * (2 * n + 5) / 18
                if s > 0:
                    z = (s - 1) / (var_s ** 0.5)
                elif s < 0:
                    z = (s + 1) / (var_s ** 0.5)
                else:
                    z = 0
                if z > 1.96:
                    analysis["trend"] = "上升"
                    analysis["trend_confidence"] = "显著"
                elif z < -1.96:
                    analysis["trend"] = "下降"
                    analysis["trend_confidence"] = "显著"
                else:
                    analysis["trend"] = "平稳"
                    analysis["trend_confidence"] = "不显著"
                analysis["mann_kendall_z"] = round(z, 3)
            else:
                mid = n // 2
                first_half = sum(qtys[:mid]) / mid if mid > 0 else 0
                second_half = sum(qtys[mid:]) / (n - mid) if (n - mid) > 0 else 0
                if second_half > first_half * 1.1:
                    analysis["trend"] = "上升"
                elif second_half < first_half * 0.9:
                    analysis["trend"] = "下降"
                else:
                    analysis["trend"] = "平稳"
                analysis["trend_confidence"] = "数据不足，简单估算"

            # ---- Seasonal detection: ACF 自相关 ----
            if n >= 14:
                try:
                    import numpy as np

                    arr = np.array(qtys, dtype=float)
                    arr_mean = arr.mean()
                    arr_centered = arr - arr_mean
                    var = (arr_centered ** 2).sum()
                    if var > 0:
                        max_lag = min(n // 2, 14)
                        acf_values = []
                        for lag in range(1, max_lag + 1):
                            autocorr = (arr_centered[:-lag] * arr_centered[lag:]).sum() / var
                            acf_values.append(round(float(autocorr), 3))

                        if acf_values:
                            best_lag = acf_values.index(max(acf_values)) + 1
                            best_acf = max(acf_values)
                            analysis["seasonal_detected"] = best_acf > 0.3
                            analysis["seasonal_period"] = best_lag if best_acf > 0.3 else None
                            analysis["seasonal_strength"] = round(best_acf, 3)
                            analysis["acf_top_lags"] = {
                                f"lag_{i + 1}": v for i, v in enumerate(acf_values[:7])
                            }
                        else:
                            analysis["seasonal_detected"] = False
                    else:
                        analysis["seasonal_detected"] = False
                        analysis["seasonal_note"] = "数据方差为0，无法检测季节性"
                except Exception as e:
                    analysis["seasonal_error"] = str(e)
            else:
                analysis["seasonal_detected"] = False
                analysis["seasonal_note"] = f"数据点不足 (n={n}，至少需要14个)"

            # ---- 推荐预设 ----
            recommendations = []
            if analysis.get("seasonal_detected"):
                sp = analysis.get("seasonal_period", 7)
                recommendations.append(f"holt_winters (检测到季节性，周期≈{sp}天)")
            if analysis.get("trend") in ("上升", "下降") and analysis.get("trend_confidence") == "显著":
                recommendations.append("arima (趋势显著，ARIMA 自动选参)")
                recommendations.append("linear_trend (简单线性趋势)")
            if std_q / mean_q < 0.3 if mean_q > 0 else True:
                recommendations.append("moving_average (需求平稳，波动小)")
            if not recommendations:
                recommendations.append("zero_shot (自动选择最佳模型)")
                recommendations.append("chronos (ARIMA 自动选参)")
            analysis["recommended_presets"] = recommendations

            # ---- 自动推荐参数 ----
            analysis["suggested_params"] = _suggest_params(qtys, analysis)

        analyses.append(analysis)

    return json.dumps({"records_analyzed": len(analyses), "analyses": analyses}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: run_trial_calculation
# ---------------------------------------------------------------------------

@_register("run_trial_calculation", {
    "type": "function",
    "function": {
        "name": "run_trial_calculation",
        "description": "用临时公式试算数据，返回计算结果给用户看效果。支持简单的 DSL 表达式",
        "parameters": {
            "type": "object",
            "properties": {
                "dsl_expression": {
                    "type": "string",
                    "description": "DSL 表达式，如 'moving_average(demand, 7)'",
                },
            },
            "required": ["dsl_expression"],
        },
    },
}, is_async=True)
async def _run_trial(args: dict) -> str:
    """使用简易 DSL 求值器执行试算。"""
    dsl_expr = args.get("dsl_expression", "")
    inputs = _current_inputs.get()
    if not dsl_expr or not inputs:
        return json.dumps({"error": "缺少 DSL 表达式或输入数据"})

    from forecast.skills.dsl import eval_dsl

    results = []
    for rec in inputs[:3]:
        try:
            result = eval_dsl(dsl_expr, rec)
            results.append({
                **_identity(rec),
                "expression": dsl_expr,
                "result_preview": str(result)[:200] if result is not None else "None",
            })
        except Exception as e:
            results.append({
                **_identity(rec),
                "error": str(e),
            })

    return json.dumps({"trial_results": results}, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Tool: compare_presets
# ---------------------------------------------------------------------------

@_register("compare_presets", {
    "type": "function",
    "function": {
        "name": "compare_presets",
        "description": "同时运行多个预设方法并对比结果，帮助用户选择最优方案。返回每个预设的预测总量、均值、标准差等统计",
        "parameters": {
            "type": "object",
            "properties": {
                "preset_names": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要对比的预设名称列表，如 ['moving_average', 'holt_winters', 'arima']",
                },
            },
            "required": ["preset_names"],
        },
    },
})
def _compare_presets(args: dict) -> str:
    """同时运行多个预设方法并对比结果。"""
    preset_names = args.get("preset_names", [])
    if not preset_names:
        return json.dumps({"error": "请提供至少一个预设名称"})

    from forecast.skills.presets import get_preset_info, run_preset

    inputs = _current_inputs.get()
    if not inputs:
        return json.dumps({"error": "没有加载输入数据。"})

    info = get_preset_info()
    all_valid: set[str] = set()
    for p in info:
        all_valid.add(p["name"])
        all_valid.update(p.get("aliases", []))
    invalid = [n for n in preset_names if n not in all_valid]
    if invalid:
        available = {p["name"] for p in info}
        return json.dumps({"error": f"不存在的预设: {invalid}。可用: {sorted(available)}"})

    comparisons = []
    for rec in inputs[:3]:
        identity = _identity(rec)
        label = " ".join(str(v) for v in identity.values() if v).strip() or "未知"
        record_result = {"identity": identity, "label": label, "presets": {}}
        for pname in preset_names:
            try:
                result = run_preset(pname, rec)
                qtys = [r.get("qty", 0) for r in result]
                record_result["presets"][pname] = {
                    "total_forecast": round(sum(qtys), 1),
                    "daily_avg": round(sum(qtys) / len(qtys), 1) if qtys else 0,
                    "max_day": max(qtys) if qtys else 0,
                    "min_day": min(qtys) if qtys else 0,
                    "days": len(qtys),
                    "first_3_days": qtys[:3],
                    "result_type": "with_balance" if result and "balance" in result[0] else "qty_only",
                }
            except Exception as e:
                record_result["presets"][pname] = {"error": str(e)}
        comparisons.append(record_result)

    summary_lines = []
    if len(comparisons) >= 2 and len(preset_names) >= 2:
        for pname in preset_names:
            totals = []
            for comp in comparisons:
                pdata = comp["presets"].get(pname, {})
                if "total_forecast" in pdata:
                    totals.append(pdata["total_forecast"])
            if totals:
                avg_total = sum(totals) / len(totals)
                summary_lines.append(f"{pname}: 预测总量均值={avg_total:.0f}")

    return json.dumps({
        "compared_presets": preset_names,
        "records_compared": len(comparisons),
        "comparisons": comparisons,
        "summary": "\n".join(summary_lines) if len(comparisons) >= 2 else None,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: suggest_skill_draft
# ---------------------------------------------------------------------------

@_register("suggest_skill_draft", {
    "type": "function",
    "function": {
        "name": "suggest_skill_draft",
        "description": "根据当前对话内容，生成一个预测 skill 草稿（DSL 表达式或 Python 代码）",
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "skill 名称"},
                "skill_type": {
                    "type": "string",
                    "enum": ["dsl", "python", "preset"],
                    "description": "skill 类型",
                },
                "dsl_expression": {"type": "string", "description": "DSL 表达式（skill_type=dsl 时填写）"},
                "python_code": {"type": "string", "description": "Python 代码（skill_type=python 时填写）"},
                "preset_name": {"type": "string", "description": "预设方法名（skill_type=preset 时填写）"},
                "description": {"type": "string", "description": "skill 的功能描述"},
            },
            "required": ["skill_name", "skill_type", "description"],
        },
    },
})
def _suggest_skill(args: dict) -> str:
    """记录 Agent 的 Skill 建议 — 实际保存由编排器负责。"""
    return json.dumps({
        "status": "draft_recorded",
        "skill": {
            "name": args.get("skill_name", ""),
            "skill_type": args.get("skill_type", "dsl"),
            "dsl_expression": args.get("dsl_expression"),
            "python_code": args.get("python_code"),
            "preset_name": args.get("preset_name"),
            "description": args.get("description", ""),
        },
        "message": "Skill 草稿已记录。用户确认后会保存为正式 skill。",
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: search_memory
# ---------------------------------------------------------------------------

@_register("search_memory", {
    "type": "function",
    "function": {
        "name": "search_memory",
        "description": "搜索历史相关的 skill 和对话，提供上下文参考",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    },
})
def _search_memory(args: dict) -> str:
    """对历史会话记录进行关键词搜索。"""
    query = args.get("query", "")
    current_session_id = _current_session_id.get()
    results = search_memory(query, limit=5, exclude_session_id=current_session_id or None)
    return json.dumps({"results": results}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool: evaluate_forecast_accuracy
# ---------------------------------------------------------------------------

@_register("evaluate_forecast_accuracy", {
    "type": "function",
    "function": {
        "name": "evaluate_forecast_accuracy",
        "description": "评估预测准确度：对比预测值和实际值，计算 MAE / MAPE / RMSE / sMAPE 指标",
        "parameters": {
            "type": "object",
            "properties": {
                "forecast": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "日期 (YYYY-MM-DD)"},
                            "qty": {"type": "number", "description": "预测值"},
                        },
                        "required": ["date", "qty"],
                    },
                    "description": "预测值时间序列",
                },
                "actual": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "日期 (YYYY-MM-DD)"},
                            "qty": {"type": "number", "description": "实际值"},
                        },
                        "required": ["date", "qty"],
                    },
                    "description": "实际值时间序列",
                },
            },
            "required": ["forecast", "actual"],
        },
    },
})
def _evaluate_accuracy(args: dict) -> str:
    """对比预测值和实际值，计算准确度指标。"""
    from datetime import date as dt_date

    from forecast.core.accuracy import compute_accuracy
    from forecast.models.forecast import TimeSeriesPoint

    forecast_raw = args.get("forecast", [])
    actual_raw = args.get("actual", [])

    if not forecast_raw or not actual_raw:
        return json.dumps({"error": "请提供 forecast 和 actual 时间序列数据"})

    try:
        forecast_points = [
            TimeSeriesPoint(date=dt_date.fromisoformat(p["date"]), qty=float(p["qty"]))
            for p in forecast_raw
        ]
        actual_points = [
            TimeSeriesPoint(date=dt_date.fromisoformat(p["date"]), qty=float(p["qty"]))
            for p in actual_raw
        ]
    except (KeyError, ValueError, TypeError) as e:
        return json.dumps({"error": f"数据格式错误，需要 date (YYYY-MM-DD) 和 qty (数值): {e}"})

    metrics = compute_accuracy(forecast_points, actual_points)
    return json.dumps(metrics.model_dump(), ensure_ascii=False)


# ---------------------------------------------------------------------------
# 自动构建 TOOL_DEFINITIONS
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = _build_tool_definitions()

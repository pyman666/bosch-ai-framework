"""图表配置生成 — 根据数据特征输出 ECharts option JSON."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from infra.llm import chat

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 图表生成 Prompt
# ---------------------------------------------------------------------------
CHART_SYSTEM_PROMPT = """你是一个 ECharts 图表配置生成器。
根据提供的 JSON 数据，生成一个标准 ECharts option JSON。

## 规则
- 必须输出纯 JSON，不包含 markdown 代码块标记
- 使用 ECharts 5.x 标准属性
- 时间序列数据 → type: "line"
- 类别对比 → type: "bar"
- 占比数据 → type: "pie"
- 颜色方案：['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452', '#9a60b4']
- 含 tooltip、legend、toolbox（saveAsImage）
- 数值较大时 tooltip 加千分位格式化
- 标题简洁，不超过 15 字

## 输出格式
严格输出如下 JSON，不要附加任何文字：
{
  "type": "bar" | "line" | "pie",
  "title": "图表标题",
  "option": { ... }
}
"""


async def generate_chart(data: list[dict], question: str) -> dict[str, Any]:
    """根据数据生成 ECharts 图表配置.

    Args:
        data: BFF 返回的数据列表
        question: 原始用户问题

    Returns:
        {"type": "bar", "option": {...}, "title": "..."}
    """
    if not data:
        return {"type": "bar", "option": {}, "title": "无数据"}

    messages = [
        {"role": "system", "content": CHART_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"用户问题: {question}\n"
                f"数据 (共 {len(data)} 条):\n"
                f"{_format_data(data)}"
            ),
        },
    ]

    response = await chat(messages)
    raw = response.get("content", "")
    parsed = _parse_chart_json(raw)
    if parsed:
        return parsed

    # LLM 输出解析失败，降级
    log.warning("[chart] LLM JSON 解析失败，使用降级方案")
    return {"type": "bar", "option": _default_bar_option(data), "title": "自动生成图表"}


def _parse_chart_json(raw: str) -> dict | None:
    """从 LLM 回复中提取并解析图表 JSON."""
    # 尝试直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 尝试提取 ```json ... ``` 代码块
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试提取第一个 {...}
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _format_data(data: list[dict], max_rows: int = 30) -> str:
    """格式化数据为 LLM 友好的文本."""
    if len(data) <= max_rows:
        return json.dumps(data, ensure_ascii=False, indent=2)
    return (
        json.dumps(data[:max_rows], ensure_ascii=False, indent=2)
        + f"\n... 共 {len(data)} 条，仅展示前 {max_rows} 条"
    )


def _default_bar_option(data: list[dict]) -> dict:
    """降级 bar chart option（LLM 解析失败时用）."""
    keys = list(data[0].keys()) if data else []
    # 找一个 category 列和一个 value 列
    cat_key = keys[0] if keys else None
    val_key = None
    for k in keys[1:]:
        if isinstance(data[0].get(k), (int, float)):
            val_key = k
            break

    return {
        "tooltip": {"trigger": "axis"},
        "toolbox": {"feature": {"saveAsImage": {}}},
        "xAxis": {
            "type": "category",
            "data": [str(r.get(cat_key, "")) for r in data] if cat_key else [],
        },
        "yAxis": {"type": "value"},
        "series": [{
            "data": [r.get(val_key, 0) for r in data] if val_key else [],
            "type": "bar",
        }],
    }
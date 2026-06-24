"""BI Agent — 基于 infra.agent.BaseAgent 的工具调用 Agent.

职责:
- 声明式定义系统提示词 + Tool 注册表
- 流式/非流式代理到 infra Agent 循环
- 在 infra 基础上添加图表生成、数据洞察等 BI 专属后处理
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

from infra.agent import BaseAgent, AgentLoopConfig
from infra.agent.tool import ToolRegistry

from analytics.core.tools import ALL_TOOLS
from analytics.core.session import get_session
from analytics.core.mock_bff import handle_mock
from analytics.core.chart import generate_chart

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

_USE_MOCK = os.environ.get("ABI_USE_MOCK", "true").lower() in ("true", "1", "yes")
_MAX_TOOL_CALLS = 5

SYSTEM_PROMPT = """你是一个 BI 数据助手，职责是帮用户用自然语言查询业务数据并生成可视化图表。

## 工作流程
1. **理解意图** — 从用户问题中提取：时间范围、指标、聚合维度、筛选条件
2. **反问澄清** — 如果缺少关键信息（如没说时间范围），先反问用户，不要猜
3. **调用接口** — 选择最匹配的 tool 获取数据
4. **分析回复** — 基于数据生成文字总结 + 图表配置

## 时间规则
- 当前日期：2026-06-05
- "上个月" = 2026-05-01 ~ 2026-05-31
- "本月" = 2026-06-01 ~ 2026-06-05
- "本周" = 2026-06-02 ~ 2026-06-08（周一~周日）
- "上周" = 2026-05-26 ~ 2026-06-01
- "昨天" = 2026-06-04
- "最近7天" = 2026-05-30 ~ 2026-06-05

## 规则
- 一次只调用一个 tool，拿到结果后再决定下一步
- 如果 tool 返回 success=false，把错误信息翻译成用户能理解的话
- 数据异常时（同比/环比波动 > 20%），主动指出
- 图表类型选择：时间序列 → line，类别对比 → bar，占比 → pie，明细 → table
- 回复简洁，附带数据口径说明（来自 meta.description）
- 如果用户只是闲聊，正常回复，不调用 tool
"""

# ---------------------------------------------------------------------------
# Tool 注册 — 把 TOOL_ROUTES 路由逻辑封装进 handler
# ---------------------------------------------------------------------------

# tool name → (bff_name, api_path, method)
TOOL_ROUTES: dict[str, tuple[str, str, str]] = {
    "query_order_summary": ("order-bff", "/api/orders/summary", "GET"),
    "query_order_detail": ("order-bff", "/api/orders/detail", "GET"),
    "query_user_metrics": ("user-bff", "/api/users/metrics", "GET"),
    "query_user_cohort": ("user-bff", "/api/users/cohort", "GET"),
}


def _build_tool_registry() -> ToolRegistry:
    """构建 ToolRegistry，每个 tool 的 handler 封装 BFF 调用 + mock fallback。"""
    registry = ToolRegistry()

    for tool_def in ALL_TOOLS:
        name = tool_def["function"]["name"]

        # 闭包捕获 name 避免循环变量问题
        def _make_handler(tool_name: str) -> Any:
            async def _handler(args: dict) -> str:
                return await _execute_tool(tool_name, args)
            return _handler

        registry.register(name, tool_def, is_async=True)(_make_handler(name))

    return registry


async def _execute_tool(name: str, args: dict) -> str:
    """执行 tool 调用 — 优先真实 BFF，fallback mock。返回 JSON 字符串。"""
    if _USE_MOCK:
        mock_result = handle_mock(name, args)
        if mock_result is not None:
            return json.dumps(mock_result, ensure_ascii=False)

    if name not in TOOL_ROUTES:
        return json.dumps({"success": False, "error": f"未知工具: {name}"})

    bff_name, api_path, method = TOOL_ROUTES[name]

    try:
        from analytics.core.bff_client import get_bff
        client = get_bff(bff_name)
        if method == "GET":
            result = await client.get(api_path, params=args)
        else:
            result = await client.post(api_path, json_data=args)
        return json.dumps(result, ensure_ascii=False)
    except ValueError:
        log.info(f"[agent] BFF '{bff_name}' 未注册，使用 mock")
        mock_result = handle_mock(name, args)
        return json.dumps(mock_result or {"success": False, "error": f"BFF 未注册: {bff_name}"}, ensure_ascii=False)
    except Exception as e:
        log.error(f"[agent] Tool '{name}' 失败: {e}")
        return json.dumps({"success": False, "error": f"接口调用失败: {e}"})


# ---------------------------------------------------------------------------
# AnalyticsAgent — 声明式定义，框架执行
# ---------------------------------------------------------------------------

class AnalyticsAgent(BaseAgent):
    system_prompt = SYSTEM_PROMPT
    tools: ToolRegistry | None = _build_tool_registry()
    config = AgentLoopConfig(max_turns=_MAX_TOOL_CALLS, max_tool_calls=_MAX_TOOL_CALLS)


# ---------------------------------------------------------------------------
# 公开 API（保持与旧接口兼容）
# ---------------------------------------------------------------------------

async def run_agent(
    user_message: str,
    session_id: str,
) -> dict[str, Any]:
    """执行一轮 BI 对话（非流式）。

    返回:
        {"reply": str, "chart": dict|None, "data": list|None,
         "sources": list[str], "session_id": str, "insights": list[str]|None}
    """
    session = get_session(session_id)
    session.add_message({"role": "user", "content": user_message})

    agent = AnalyticsAgent()
    result = await agent.run(
        messages=session.get_messages(),
    )

    # 提取 tool 返回的数据
    collected_data, sources = _extract_tool_data(result.get("tool_calls", []))

    # 更新 session
    session.add_message({"role": "assistant", "content": result["content"]})

    chart = None
    insights = None
    if collected_data:
        chart = await _try_generate_chart(collected_data, user_message)
        insights = _extract_insights(collected_data)

    return {
        "reply": result["content"],
        "chart": chart,
        "data": collected_data or None,
        "sources": sources,
        "session_id": session_id,
        "insights": insights,
    }


async def run_agent_stream(
    user_message: str,
    session_id: str,
) -> AsyncIterator[str]:
    """执行 BI 对话，流式产出 SSE 事件。

    产出格式:
        event: text       data: {"type": "text", "delta": "..."}
        event: tool_call  data: {"type": "tool_call", "tool": "...", "args": {...}}
        event: tool_result data: {"type": "tool_result", "tool": "...", "success": true}
        event: chart      data: {"type": "chart", "chart": {...}}
        event: final      data: {"type": "final", "reply": "...", ...}
        data: [DONE]
    """
    session = get_session(session_id)
    session.add_message({"role": "user", "content": user_message})

    agent = AnalyticsAgent()
    text_acc = ""
    collected_data: list[dict] = []
    sources: list[str] = []

    async for event in agent.run_stream(messages=session.get_messages()):
        if event["type"] == "delta":
            text_acc += event["content"]
            yield _sse("text", {"type": "text", "delta": event["content"]})

        elif event["type"] == "tool_call":
            yield _sse("tool_call", {
                "type": "tool_call",
                "tool": event["name"],
                "args": event["args"],
            })

        elif event["type"] == "tool_result":
            tool_name = event["name"]
            try:
                parsed = json.loads(event["result"])
            except json.JSONDecodeError:
                parsed = {}
            if parsed.get("success"):
                data = parsed.get("data", [])
                if isinstance(data, list):
                    collected_data.extend(data)
                if tool_name not in sources:
                    sources.append(tool_name)

            yield _sse("tool_result", {
                "type": "tool_result",
                "tool": tool_name,
                "success": parsed.get("success", False),
            })

        elif event["type"] == "done":
            session.add_message({"role": "assistant", "content": text_acc})

            chart = None
            insights = None
            if collected_data:
                chart = await _try_generate_chart(collected_data, user_message)
                insights = _extract_insights(collected_data)
                yield _sse("chart", {"type": "chart", "chart": chart})

            yield _sse("final", {
                "type": "final",
                "reply": text_acc,
                "chart": chart,
                "data": collected_data,
                "sources": sources,
                "insights": insights,
            })
            yield "data: [DONE]\n\n"
            return

    # 超出最大轮数
    final_reply = text_acc or "抱歉，查询步骤过多，请尝试更具体的问题。"
    yield _sse("final", {
        "type": "final",
        "reply": final_reply,
        "chart": None,
        "sources": sources,
    })
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _extract_tool_data(tool_calls: list[dict]) -> tuple[list[dict], list[str]]:
    """从 tool_calls records 中提取数据和来源."""
    data: list[dict] = []
    sources: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        result_str = tc.get("result", "{}")
        try:
            result = json.loads(result_str)
        except json.JSONDecodeError:
            continue
        if result.get("success"):
            d = result.get("data", [])
            if isinstance(d, list):
                data.extend(d)
            if name not in sources:
                sources.append(name)
    return data, sources


def _sse(event: str, data: dict) -> str:
    """格式化 SSE 事件."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _try_generate_chart(data: list[dict], question: str) -> dict | None:
    """尝试生成图表，失败返回 None."""
    try:
        return await generate_chart(data, question)
    except Exception as e:
        log.error(f"[agent] 图表生成失败: {e}")
        return None


def _extract_insights(data: list[dict]) -> list[str] | None:
    """从数据中提取简单洞察（规则引擎）。"""
    if not data or len(data) < 2:
        return None

    insights: list[str] = []
    numeric_keys = [k for k, v in data[0].items() if isinstance(v, (int, float))]
    if not numeric_keys:
        return None

    for key in numeric_keys[:2]:
        values = [r.get(key, 0) for r in data if key in r]
        if not values:
            continue
        max_val = max(values)
        min_val = min(values)
        if min_val > 0 and (max_val - min_val) / min_val > 0.2:
            insights.append(f"{key} 波动较大：最高 {max_val}，最低 {min_val}")

    return insights or None

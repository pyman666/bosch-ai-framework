"""Agent 循环 — 意图识别 → Tool Use → 图表生成.

支持:
- 多轮对话（session 上下文保持）
- 流式 SSE 输出（中间思考 + tool 调用 + 最终回复）
- Mock BFF（无真实后端时可测试）
- 错误处理 + LLM 重试
- 图表配置自动生成
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator

from infra.llm import chat, chat_stream
from analytics.core.tools import get_tools
from analytics.core.session import get_session
from analytics.core.mock_bff import handle_mock
from analytics.core.chart import generate_chart

log = logging.getLogger(__name__)

# 是否使用 mock 数据（无真实 BFF 时自动 fallback，或显式开启）
_USE_MOCK = os.environ.get("ABI_USE_MOCK", "true").lower() in ("true", "1", "yes")

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

# tool 调用失败时最多重试次数
_MAX_TOOL_RETRIES = 2
# 一个 agent 循环最多执行的 tool 调用次数（防止死循环）
_MAX_TOOL_CALLS = 5


# ============================================================================
# 非流式 Agent
# ============================================================================

async def run_agent(
    user_message: str,
    session_id: str,
) -> dict[str, Any]:
    """执行一轮 Agent 对话（非流式）.

    返回:
        {"reply": str, "chart": dict|None, "data": list|None,
         "sources": list[str], "session_id": str, "insights": list[str]|None}
    """
    session = get_session(session_id)
    session.add_message({"role": "user", "content": user_message})

    tools = get_tools()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + session.get_messages()

    tool_call_count = 0
    collected_data: list[dict] = []
    sources: list[str] = []

    while tool_call_count < _MAX_TOOL_CALLS:
        response = await chat(messages, tools=tools)

        if not response.get("tool_calls"):
            # 没有 tool call — 最终回复（反问或总结）
            session.add_message({"role": "assistant", "content": response["content"]})

            chart = None
            insights = None
            if collected_data:
                chart = await _try_generate_chart(collected_data, user_message)
                insights = _extract_insights(collected_data)

            return {
                "reply": response["content"],
                "chart": chart,
                "data": collected_data or None,
                "sources": sources,
                "session_id": session_id,
                "insights": insights,
            }

        # 有 tool calls — 执行并继续
        messages.append(_build_assistant_msg(response))

        for tc in response["tool_calls"]:
            tool_call_count += 1
            tool_name = tc["function"]["name"]
            tool_args = _safe_parse_args(tc["function"]["arguments"])
            log.info(f"[agent] Tool call #{tool_call_count}: {tool_name}({tool_args})")

            tool_result = await _execute_tool(tool_name, tool_args)

            if tool_result.get("success"):
                collected_data.extend(tool_result.get("data", []))
                if tool_name not in sources:
                    sources.append(tool_name)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(tool_result, ensure_ascii=False),
            })

    # 超出最大调用次数 — 强制结束
    log.warning(f"[agent] Hit max tool calls ({_MAX_TOOL_CALLS}), forcing final response")
    final = await chat(messages, tools=None)
    session.add_message({"role": "assistant", "content": final["content"]})
    return {
        "reply": final["content"],
        "chart": None,
        "data": collected_data or None,
        "sources": sources,
        "session_id": session_id,
        "insights": None,
    }


# ============================================================================
# 流式 Agent（SSE）
# ============================================================================

async def run_agent_stream(
    user_message: str,
    session_id: str,
) -> AsyncIterator[str]:
    """执行 Agent 对话，流式产出 SSE 事件.

    产出格式:
        event: text
        data: {"type": "text", "delta": "..."}

        event: tool_call
        data: {"type": "tool_call", "tool": "...", "args": {...}}

        event: tool_result
        data: {"type": "tool_result", "tool": "...", "success": true}

        event: chart
        data: {"type": "chart", "chart": {...}}

        event: final
        data: {"type": "final", "reply": "...", "chart": {...}, "insights": [...]}

        data: [DONE]
    """
    session = get_session(session_id)
    session.add_message({"role": "user", "content": user_message})

    tools = get_tools()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + session.get_messages()

    tool_call_count = 0
    collected_data: list[dict] = []
    sources: list[str] = []
    text_acc = ""  # 累积纯文字回复

    while tool_call_count < _MAX_TOOL_CALLS:
        # 流式调用 LLM — chat_stream 内部已处理 tool_call 累积
        final_tool_calls = None
        content_acc = ""

        async for chunk in chat_stream(messages, tools=tools):
            if "delta" in chunk:
                content_acc += chunk["delta"]
                text_acc += chunk["delta"]
                yield _sse_event("text", {"type": "text", "delta": chunk["delta"]})

            if "finish" in chunk and chunk.get("tool_calls"):
                final_tool_calls = chunk["tool_calls"]

        # 检查是否有 tool calls（chat_stream 在 finish 时产出完整列表）
        if final_tool_calls:
            messages.append({
                "role": "assistant",
                "content": content_acc,
                "tool_calls": final_tool_calls,
            })

            for tc in final_tool_calls:
                tool_call_count += 1
                tool_name = tc["function"]["name"]
                tool_args = _safe_parse_args(tc["function"]["arguments"])

                yield _sse_event("tool_call", {
                    "type": "tool_call",
                    "tool": tool_name,
                    "args": tool_args,
                })

                tool_result = await _execute_tool(tool_name, tool_args)

                if tool_result.get("success"):
                    collected_data.extend(tool_result.get("data", []))
                    if tool_name not in sources:
                        sources.append(tool_name)

                yield _sse_event("tool_result", {
                    "type": "tool_result",
                    "tool": tool_name,
                    "success": tool_result.get("success", False),
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result, ensure_ascii=False),
                })
            continue

        # 没有 tool call — 最终回复
        session.add_message({"role": "assistant", "content": text_acc})

        # 生成图表
        chart = None
        insights = None
        if collected_data:
            chart = await _try_generate_chart(collected_data, user_message)
            insights = _extract_insights(collected_data)
            yield _sse_event("chart", {
                "type": "chart",
                "chart": chart,
            })

        yield _sse_event("final", {
            "type": "final",
            "reply": text_acc,
            "chart": chart,
            "data": collected_data,
            "sources": sources,
            "insights": insights,
        })
        yield "data: [DONE]\n\n"
        return

    # 超出最大调用次数
    final_reply = text_acc or "抱歉，查询步骤过多，请尝试更具体的问题。"
    yield _sse_event("final", {
        "type": "final",
        "reply": final_reply,
        "chart": None,
        "sources": sources,
    })
    yield "data: [DONE]\n\n"


# ============================================================================
# Tool 执行
# ============================================================================

# tool name → (bff_name, api_path, method)
TOOL_ROUTES: dict[str, tuple[str, str, str]] = {
    "query_order_summary": ("order-bff", "/api/orders/summary", "GET"),
    "query_order_detail": ("order-bff", "/api/orders/detail", "GET"),
    "query_user_metrics": ("user-bff", "/api/users/metrics", "GET"),
    "query_user_cohort": ("user-bff", "/api/users/cohort", "GET"),
}


async def _execute_tool(name: str, args: dict) -> dict:
    """执行 tool 调用 — 优先尝试真实 BFF，fallback 到 mock."""
    if _USE_MOCK:
        mock_result = handle_mock(name, args)
        if mock_result is not None:
            return mock_result

    if name not in TOOL_ROUTES:
        return {"success": False, "error": f"未知工具: {name}"}

    bff_name, api_path, method = TOOL_ROUTES[name]

    for attempt in range(1, _MAX_TOOL_RETRIES + 1):
        try:
            from analytics.core.bff_client import get_bff
            client = get_bff(bff_name)
            if method == "GET":
                return await client.get(api_path, params=args)
            else:
                return await client.post(api_path, json_data=args)
        except ValueError:
            # BFF 未注册，fallback mock
            log.info(f"[agent] BFF '{bff_name}' 未注册，使用 mock 数据")
            mock_result = handle_mock(name, args)
            return mock_result or {"success": False, "error": f"BFF 未注册: {bff_name}"}
        except Exception as e:
            log.error(f"[agent] Tool '{name}' 第 {attempt} 次失败: {e}")
            if attempt == _MAX_TOOL_RETRIES:
                return {"success": False, "error": f"接口调用失败: {e}"}


# ============================================================================
# 辅助函数
# ============================================================================

def _build_assistant_msg(response: dict) -> dict:
    """构建 assistant message（含 tool_calls）."""
    return {
        "role": "assistant",
        "content": response.get("content", ""),
        "tool_calls": response.get("tool_calls"),
    }


def _safe_parse_args(raw: str) -> dict:
    """安全解析 tool arguments JSON."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"[agent] Tool arguments JSON 解析失败: {raw[:200]}")
        return {}


async def _try_generate_chart(data: list[dict], question: str) -> dict | None:
    """尝试生成图表，失败返回 None（不阻断主流程）."""
    try:
        return await generate_chart(data, question)
    except Exception as e:
        log.error(f"[agent] 图表生成失败: {e}")
        return None


def _extract_insights(data: list[dict]) -> list[str] | None:
    """从数据中提取简单洞察（规则引擎，后续可接入 LLM）."""
    if not data or len(data) < 2:
        return None

    insights: list[str] = []
    # 数值列自动求同比
    numeric_keys = [k for k, v in data[0].items() if isinstance(v, (int, float))]
    if not numeric_keys:
        return None

    # 简单的最大/最小值洞察
    for key in numeric_keys[:2]:  # 最多看 2 个指标
        values = [r.get(key, 0) for r in data if key in r]
        if not values:
            continue
        max_val = max(values)
        min_val = min(values)
        if min_val > 0 and (max_val - min_val) / min_val > 0.2:
            insights.append(f"{key} 波动较大：最高 {max_val}，最低 {min_val}")

    return insights or None


def _sse_event(event: str, data: dict) -> str:
    """格式化 SSE 事件."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

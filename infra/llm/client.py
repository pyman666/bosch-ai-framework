"""LLM 调用 API — 业务代码唯一入口.

屏蔽 LiteLLM 实现细节，只暴露三个函数:

    chat()        非流式调用
    stream()      流式调用 (SSE)
    chat_stream() stream 的别名 (向后兼容)

以后换 OpenAI SDK / SAP AI Core 只改 router.py，这个文件不动。
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from infra.llm.router import _DEFAULT_MODEL, get_router

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def stream(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """流式调用 LLM, yield 增量事件.

    Yields:
        ``{"delta": str}`` — 文本增量
        ``{"finish": str, "tool_calls": [...]}`` — 结束 + 完整 tool_calls
    """
    router = get_router()
    response = await router.acompletion(
        model=model or _DEFAULT_MODEL,
        messages=messages,
        tools=tools,
        stream=True,
        stream_options={"include_usage": True},
    )

    tool_call_acc: dict[int, dict[str, Any]] = {}
    async for chunk in response:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta is None:
            continue

        if delta.content:
            yield {"delta": delta.content}

        if delta.tool_calls:
            for tc in delta.tool_calls:
                idx = tc.index
                if idx not in tool_call_acc:
                    tool_call_acc[idx] = {
                        "id": tc.id or "",
                        "function": {"name": "", "arguments": ""},
                    }
                acc = tool_call_acc[idx]
                if tc.id:
                    acc["id"] = tc.id
                if tc.function:
                    if tc.function.name:
                        acc["function"]["name"] += tc.function.name
                    if tc.function.arguments:
                        acc["function"]["arguments"] += tc.function.arguments

        if chunk.choices[0].finish_reason:
            final_tool_calls = list(tool_call_acc.values()) if tool_call_acc else None
            yield {"finish": chunk.choices[0].finish_reason, "tool_calls": final_tool_calls}
            return


# 向后兼容别名
chat_stream = stream


# ---------------------------------------------------------------------------
# Non-streaming
# ---------------------------------------------------------------------------


async def chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """非流式调用 LLM, 返回完整响应.

    Returns:
        {
            "content": str,
            "tool_calls": [{"id": str, "function": {"name": str, "arguments": str}}],
            "finish_reason": str,
        }
    """
    router = get_router()
    response = await router.acompletion(
        model=model or _DEFAULT_MODEL,
        messages=messages,
        tools=tools,
    )
    choice = response.choices[0]
    msg = choice.message
    return {
        "content": msg.content or "",
        "tool_calls": [
            {
                "id": tc.id,
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in (msg.tool_calls or [])
        ],
        "finish_reason": choice.finish_reason,
    }

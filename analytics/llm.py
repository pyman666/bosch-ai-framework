"""LLM 服务 — LiteLLM Router 封装."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator
from litellm import Router

from analytics.settings import DEFAULT_MODEL, MODEL_LIST, ROUTER_KWARGS

log = logging.getLogger(__name__)

_router: Router | None = None


def get_router() -> Router:
    global _router
    if _router is None:
        _router = Router(model_list=MODEL_LIST, **ROUTER_KWARGS)
    return _router


async def chat_stream(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    router = get_router()
    response = await router.acompletion(
        model=model or DEFAULT_MODEL,
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
            final_tool_calls = (
                list(tool_call_acc.values()) if tool_call_acc else None
            )
            yield {"finish": chunk.choices[0].finish_reason, "tool_calls": final_tool_calls}
            return


async def chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    router = get_router()
    response = await router.acompletion(
        model=model or DEFAULT_MODEL,
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

"""LLM 服务 — LiteLLM Router 封装.

提供统一的 LLM 调用入口, 支持:
- ``chat()``: 非流式调用, 返回完整响应
- ``chat_stream()``: 流式调用, 支持 tool_calls 增量聚合
- ``get_router()``: 获取 Router 实例 (用于预热)
- ``instructor_call()``: 结构化输出 (配合 instructor 库)

用法::

    from infra.llm import chat, chat_stream

    # 非流式
    result = await chat(messages=[...], tools=[...])

    # 流式
    async for chunk in chat_stream(messages=[...]):
        if "delta" in chunk:
            print(chunk["delta"])
        elif "finish" in chunk:
            print("done:", chunk["tool_calls"])
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, AsyncIterator

from litellm import Router

if TYPE_CHECKING:
    import instructor

log = logging.getLogger(__name__)

# 延迟初始化, 在首次使用时才构建
_router: Router | None = None

# 由 settings 模块注入
_MODEL_LIST: list[dict] = []
_ROUTER_KWARGS: dict = {}
_DEFAULT_MODEL: str = ""


def _configure(*, model_list: list[dict], router_kwargs: dict, default_model: str) -> None:
    """在服务启动时配置 LLM 模块 (由 infra.settings 自动调用)."""
    global _MODEL_LIST, _ROUTER_KWARGS, _DEFAULT_MODEL
    _MODEL_LIST = model_list
    _ROUTER_KWARGS = router_kwargs
    _DEFAULT_MODEL = default_model


def get_router() -> Router:
    """获取 (或创建) 全局 Router 实例."""
    global _router
    if _router is None:
        if not _MODEL_LIST:
            raise RuntimeError(
                "LLM 未配置: 请先调用 infra.settings.load_config() 或 "
                "确保在导入 infra.llm 之前已导入 infra.settings"
            )
        _router = Router(model_list=_MODEL_LIST, **_ROUTER_KWARGS)
    return _router


async def chat_stream(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """流式调用 LLM, yield 增量内容.

    Yields:
        - ``{"delta": str}`` -- 文本增量
        - ``{"finish": str, "tool_calls": [...]}`` -- 结束标记 + 完整 tool_calls
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
    """非流式调用 LLM, 返回完整响应."""
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


# ---------------------------------------------------------------------------
# Instructor 支持 (可选, 仅在安装 instructor 时可用)
# ---------------------------------------------------------------------------

_instructor_client: "instructor.AsyncInstructor | None" = None


def get_instructor_client() -> "instructor.AsyncInstructor | None":
    """获取 instructor 客户端 (惰性初始化)."""
    global _instructor_client
    if _instructor_client is None:
        try:
            import instructor as _instructor
        except ImportError:
            log.warning("instructor 未安装, instructor_call 不可用")
            return None
        router = get_router()
        _instructor_client = _instructor.from_litellm(
            router.acompletion,
            mode=_instructor.Mode.JSON,
        )
    return _instructor_client


async def instructor_call(
    schema: type,
    messages: list[dict],
    *,
    model: str,
    retry: int = 2,
) -> Any:
    """结构化输出调用 (需要安装 instructor 包).

    Args:
        schema: Pydantic model class
        messages: 对话消息列表
        model: 模型名
        retry: 重试次数

    Raises:
        HTTPException: 调用失败时
    """
    from fastapi import HTTPException
    from litellm.exceptions import APIError

    client = get_instructor_client()
    if client is None:
        raise RuntimeError("instructor 未安装, 无法使用 instructor_call")

    try:
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            response_model=schema,
            max_retries=max(retry, 0),
        )
    except APIError as e:
        raise HTTPException(
            status_code=getattr(e, "status_code", 500),
            detail=getattr(e, "message", str(e)),
        ) from e

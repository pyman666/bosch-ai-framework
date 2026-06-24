"""LLM 抽象层 — 业务代码唯一入口.

用法::

    from infra.llm import chat, stream

    # 非流式
    result = await chat(messages=[...], tools=[...])

    # 流式
    async for chunk in stream(messages=[...]):
        if "delta" in chunk:
            print(chunk["delta"])

屏蔽 LiteLLM 实现细节。以后换 OpenAI SDK / SAP AI Core 只改 router.py，
client.py 和所有业务代码不动。
"""

from infra.llm.client import chat, chat_stream, stream
from infra.llm.router import (
    _configure,
    get_instructor_client,
    get_router,
    instructor_call,
)

# Module-level lazy accessors — match document.llm pattern
_router = None
_aclient = None


def __getattr__(name: str):
    global _router, _aclient
    if name == "router":
        if _router is None:
            _router = get_router()
        return _router
    if name == "aclient":
        if _aclient is None:
            _aclient = get_instructor_client()
        return _aclient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


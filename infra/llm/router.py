"""LiteLLM Router 生命周期管理.

- ``_configure()``: settings 注入模型列表和参数
- ``get_router()``: 惰性初始化全局 Router
- ``get_instructor_client()`` / ``instructor_call()``: 结构化输出

以后换 OpenAI SDK / SAP AI Core 只改这个文件，client.py 和业务代码不受影响。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from litellm import Router

if TYPE_CHECKING:
    import instructor

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Router 状态 (由 settings 模块注入)
# ---------------------------------------------------------------------------

_router: Router | None = None
_MODEL_LIST: list[dict] = []
_ROUTER_KWARGS: dict = {}
_DEFAULT_MODEL: str = ""


def _configure(*, model_list: list[dict], router_kwargs: dict, default_model: str) -> None:
    """在服务启动时配置 LLM 模块 (由 infra.config.settings 自动调用)."""
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
                "LLM 未配置: 请先调用 infra.config.settings.load_config() 或 "
                "确保在导入 infra.llm 之前已导入 infra.config.settings"
            )
        _router = Router(model_list=_MODEL_LIST, **_ROUTER_KWARGS)
    return _router


# ---------------------------------------------------------------------------
# Instructor (结构化输出)
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
    """结构化输出调用 (需要安装 instructor 包)."""
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

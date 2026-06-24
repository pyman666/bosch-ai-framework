"""共享的 LLM 客户端入口.

所有需要走 LLM 的子模块 (pdf VLM / excel planner / 后续可能的 agent ...) 都在这里
拿同一个 ``aclient``. 走同一个 LiteLLM Router 才能让 ``settings.yaml`` 里的
配额/重试/key 轮转策略生效.

调用方:
    from document.llm import aclient
    obj = await aclient.chat.completions.create(model=..., response_model=Schema, ...)
"""
import litellm
import instructor
from fastapi import HTTPException
from litellm import Router
from litellm.exceptions import APIError
from pydantic import BaseModel
from typing import TypeVar

from .settings import MODEL_LIST, ROUTER_KWARGS


T = TypeVar("T", bound=BaseModel)


# 让 LiteLLM 在底层 provider 不支持某些 OpenAI 参数 (e.g. response_format) 时静默丢弃,
# 而不是直接报错. instructor 自己也会按 mode 选合适的字段.
litellm.drop_params = True


router: Router = Router(model_list=MODEL_LIST, **ROUTER_KWARGS)
aclient: instructor.AsyncInstructor = instructor.from_litellm(
    router.acompletion,
    mode=instructor.Mode.JSON,
)


async def instructor_call(
    schema: type[T],
    messages: list[dict],
    *,
    model: str,
    retry: int = 2,
) -> T:
    try:
        return await aclient.chat.completions.create(
            model=model,
            messages=messages,
            response_model=schema,
            max_retries=max(retry, 0),
        )
    except APIError as e:
        raise HTTPException(
            status_code=getattr(e, "status_code", 500),
            detail=getattr(e, "message", str(e)),
        )


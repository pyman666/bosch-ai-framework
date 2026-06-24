"""失败诊断: 哪些异常不要送 LLM, 哪些要送 LLM 出 prose 给客户."""
import json
import logging
from typing import Awaitable, TypeVar

import litellm.exceptions as _lle
from instructor.core import InstructorRetryException

from ..llm import router as _router
from ..settings import DEFAULT_MODEL
from .handler import BusinessFailure, ChatHandler


logger = logging.getLogger(__name__)
T = TypeVar("T")


# 这些异常**不送** LLM 诊断, 直接抛给上游 (Java 后端按 HTTP 5xx 处理).
# 都是 "AI 自己跑不动" 或 "infra 问题, AI 答不出" 的类型.
NO_DIAGNOSE: tuple[type[BaseException], ...] = (
    _lle.RateLimitError,
    _lle.AuthenticationError,
    _lle.ServiceUnavailableError,
    _lle.APIConnectionError,
    InstructorRetryException,
    OSError,
    MemoryError,
)


async def safe_run(
    coro: Awaitable[T],
    *,
    handler: ChatHandler,
    ctx: dict | None = None,
) -> tuple[T | None, str | None]:
    """跑 ``coro``, 把"该送 LLM 诊断的失败"统一转成 ``(None, diagnosis)``.

    返回值:
        - 成功 -> ``(result, None)``
        - 业务软失败 (``BusinessFailure``) / 其他代码异常 -> ``(None, diagnosis_text)``
        - 基础设施失败 (``NO_DIAGNOSE`` 名单) -> 直接 ``raise``
    """
    try:
        return await coro, None
    except NO_DIAGNOSE:
        raise
    except BusinessFailure as e:
        logger.warning("business failure in %s: %s", handler.name, e.reason)
        diag = await diagnose_failure(
            handler,
            failure_kind="business",
            failure={"reason": e.reason, "context": e.ctx},
            extra_ctx=ctx,
        )
        return None, diag
    except Exception as e:
        logger.exception("unexpected failure in %s, sending to diagnose", handler.name)
        diag = await diagnose_failure(
            handler,
            failure_kind="exception",
            failure={"type": type(e).__name__, "message": str(e)},
            extra_ctx=ctx,
        )
        return None, diag


async def diagnose_failure(
    handler: ChatHandler,
    *,
    failure_kind: str,
    failure: dict,
    extra_ctx: dict | None = None,
) -> str:
    """让 LLM 看失败信息, 输出一段给业务方的中文 prose.

    刻意**不**用 instructor / 不要求结构化输出 -- 这里要的就是自然语言.
    """
    payload = {
        "scenario": handler.name,
        "failure_kind": failure_kind,
        "failure": failure,
        "context": extra_ctx or {},
    }
    user_msg = (
        "下面是本次解析失败的信息 (JSON):\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n"
        "请用一段简短的中文跟业务方对话: 用'我'称呼自己, 用'您'称呼业务方, "
        "说明可能的原因, 以及希望业务方提供什么信息可以恢复. "
        "直接输出 prose, 不要 JSON, 不要客套, 不超过 5 句话."
    )

    resp = await _router.acompletion(
        model=DEFAULT_MODEL,
        messages=[
            {"role": "system", "content": handler.diagnose_prompt},
            {"role": "user", "content": user_msg},
        ],
    )
    return resp.choices[0].message.content.strip()

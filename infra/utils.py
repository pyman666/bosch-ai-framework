"""通用工具函数."""

from __future__ import annotations

from datetime import datetime, timezone

_MISSING = object()


def exception_detail(exc: Exception) -> object:
    """从异常对象里挖出"最有信息量"的 detail.

    优先级 (从高到低):

    1. ``exc.errors`` -- pydantic ``ValidationError`` / instructor 校验错误的标准接口,
       是 list[dict], 比 ``str(exc)`` 一坨长字符串好读.
    2. ``exc.detail`` -- FastAPI ``HTTPException`` 抛出来的结构化 detail.
    3. ``str(exc)`` -- 兜底, 普通异常拿 message.

    返回类型故意标 ``object`` 而不是 ``str``: 上面三种来源结构不一样, 让调用方
    (一般是 ``TaskResult.error``) 直接 JSON 序列化即可, 不强行扁平化, 保留原始结构
    给前端排查.
    """
    errors = getattr(exc, "errors", _MISSING)
    if errors is not _MISSING:
        return errors() if callable(errors) else errors

    detail = getattr(exc, "detail", _MISSING)
    if detail is not _MISSING:
        return detail

    return str(exc)


def utcnow() -> datetime:
    """返回不带时区信息的 UTC 时间（替代已弃用的 datetime.utcnow）."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

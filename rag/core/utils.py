from typing import Any


def exception_detail(exc: Exception) -> Any:
    """从异常对象抽出适合放进响应 body 的内容.

    优先级: ``exc.errors()`` (Pydantic / FastAPI 校验错误)
        -> ``exc.detail`` (HTTPException)
        -> ``str(exc)`` (兜底).
    """
    errors = getattr(exc, "errors", None)
    if errors is not None:
        return errors() if callable(errors) else errors
    detail = getattr(exc, "detail", None)
    if detail is not None:
        return detail
    return str(exc)

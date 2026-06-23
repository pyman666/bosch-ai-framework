"""Instructor integration — structured output via LLM.

Optional dependency: install with ``pip install bosch-ai-framework[instructor]``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bosch_ai_framework.llm.router import get_router

if TYPE_CHECKING:
    import instructor

log = logging.getLogger(__name__)

_instructor_client: "instructor.AsyncInstructor | None" = None


def get_instructor_client() -> "instructor.AsyncInstructor | None":
    """Get instructor client (lazy init). Returns None if instructor not installed."""
    global _instructor_client
    if _instructor_client is None:
        try:
            import instructor as _instructor
        except ImportError:
            log.warning("instructor not installed, instructor_call unavailable")
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
    """Structured output call (requires instructor package).

    Args:
        schema: Pydantic model class
        messages: conversation messages
        model: model name
        retry: max retries

    Raises:
        HTTPException: on API failure
    """
    from fastapi import HTTPException
    from litellm.exceptions import APIError

    client = get_instructor_client()
    if client is None:
        raise RuntimeError("instructor not installed, cannot use instructor_call")

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

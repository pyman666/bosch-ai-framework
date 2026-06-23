"""General utility functions."""

from __future__ import annotations

from datetime import datetime, timezone

_MISSING = object()


def exception_detail(exc: Exception) -> object:
    """Extract the most informative detail from an exception.

    Priority (highest to lowest):

    1. ``exc.errors`` — Pydantic ValidationError / instructor validation errors.
       Returns list[dict], more readable than ``str(exc)``.
    2. ``exc.detail`` — FastAPI HTTPException structured detail.
    3. ``str(exc)`` — fallback, plain exception message.

    Return type is deliberately ``object`` (not ``str``) — preserves structured
    data for JSON serialization to the frontend.
    """
    errors = getattr(exc, "errors", _MISSING)
    if errors is not _MISSING:
        return errors() if callable(errors) else errors

    detail = getattr(exc, "detail", _MISSING)
    if detail is not _MISSING:
        return detail

    return str(exc)


def utcnow() -> datetime:
    """Return timezone-naive UTC datetime (replaces deprecated datetime.utcnow)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

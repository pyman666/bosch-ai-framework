"""FastAPI application factory — CORS, error handlers, middleware.

Usage::

    from bosch_ai_framework.server import create_app

    app = create_app(name="my-service")

    @app.get("/health")
    def health():
        return {"status": "ok"}
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


def create_app(
    name: str,
    *,
    version: str = "0.1.0",
    auth_dependency: Callable | None = None,
    cors_origins: list[str] | None = None,
    lifespan: Callable | None = None,
    docs_url: str = "/docs",
) -> FastAPI:
    """Create a FastAPI application with standard middleware.

    Args:
        name: Application name (used in OpenAPI title).
        version: API version string.
        auth_dependency: FastAPI dependency for auth (e.g. ``require_auth``).
            Applied via ``include_router(dependencies=[Depends(auth_dependency)])``.
        cors_origins: Allowed CORS origins, defaults to ``["*"]``.
        lifespan: Optional custom lifespan context manager.
        docs_url: OpenAPI docs URL, default ``/docs``.

    Returns:
        Configured FastAPI app instance.
    """
    if _default_lifespan is None:
        _lifespan = lifespan
    elif lifespan is not None:
        # Combine custom lifespan with default
        @asynccontextmanager
        async def _combined(app: FastAPI):
            async with _default_lifespan(app):
                async with lifespan(app):
                    yield
        _lifespan = _combined
    else:
        _lifespan = _default_lifespan

    app = FastAPI(
        title=name,
        version=version,
        lifespan=_lifespan,
        docs_url=docs_url,
    )

    # CORS — permissive by default, lock down in production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Global exception handler
    @app.exception_handler(Exception)
    async def _catch_all(request, exc: Exception):
        log.exception("Unhandled exception")
        return JSONResponse(
            status_code=500,
            content={"detail": str(exc)},
        )

    @app.get("/health")
    async def _health():
        return {"status": "ok"}

    return app


@asynccontextmanager
async def _default_lifespan(app: FastAPI):
    """Warm up LLM router on startup."""
    from bosch_ai_framework.llm.router import get_router
    get_router()
    log.info("%s started, LLM router ready", app.title)
    yield
    log.info("%s shutting down", app.title)

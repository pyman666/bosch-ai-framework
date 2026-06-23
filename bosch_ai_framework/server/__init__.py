"""Server module — FastAPI factory, logging, middleware.

Provides:
    - ``create_app()``: FastAPI app factory with CORS, error handlers, health check
"""

from bosch_ai_framework.server.app import create_app

__all__ = ["create_app"]

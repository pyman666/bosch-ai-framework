"""LLM module — LiteLLM Router wrapper.

Provides:
    - ``chat()``, ``chat_stream()``: standard LLM calls
    - ``get_router()``: get/create global Router instance
    - ``get_instructor_client()``, ``instructor_call()``: structured output (optional)
"""

from bosch_ai_framework.llm.router import (
    chat,
    chat_stream,
    get_router,
)
from bosch_ai_framework.llm.instructor import (
    get_instructor_client,
    instructor_call,
)

__all__ = [
    "chat",
    "chat_stream",
    "get_router",
    "get_instructor_client",
    "instructor_call",
]

"""Chat module — 5-state human-in-the-loop FSM orchestrator.

Install: ``pip install bosch-ai-framework[chat]`` (no extra dependencies)

Provides:
    - ``SessionState``: 5-state enum (INTENT_PREVIEW → PLANNING → AWAITING_CONFIRM → DONE / AWAITING_FEEDBACK)
    - ``ALLOWED_ENTRIES``: state transition validation table
    - ``ChatHandler``: Protocol each business client must implement
    - ``ChatPlan``: Base Pydantic model for LLM-generated plans
    - ``BusinessFailure``: Soft failure signal → LLM diagnosis
    - ``register()`` / ``get()``: Handler registry

Usage::

    from bosch_ai_framework.chat import (
        SessionState, ALLOWED_ENTRIES,
        ChatHandler, ChatPlan, BusinessFailure,
        register, get,
    )

    class MyHandler:
        name = "my-client"
        PlanSchema = MyPlan
        plan_prompt = "..."
        diagnose_prompt = "..."

        def build_skeleton(self, raw, *, sheet=1): ...
        async def execute(self, plan, raw, *, sheet=1): ...

    register(MyHandler())

Extracted from: bosch-idoc
"""

from bosch_ai_framework.chat.handler import (
    BusinessFailure,
    ChatHandler,
    ChatPlan,
)
from bosch_ai_framework.chat.registry import get, register
from bosch_ai_framework.chat.state import ALLOWED_ENTRIES, SessionState

__all__ = [
    "SessionState",
    "ALLOWED_ENTRIES",
    "ChatHandler",
    "ChatPlan",
    "BusinessFailure",
    "register",
    "get",
]

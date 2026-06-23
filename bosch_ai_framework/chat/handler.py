"""Chat handler protocol — the contract each business client must implement.

A business client connecting to the chat pipeline does three things:
    1. Define a plan schema extending ``ChatPlan``.
    2. Implement ``ChatHandler``: provide prompts, skeleton builder, and ``execute(plan, raw)``.
    3. Register the handler via ``registry.register(handler)``.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class BusinessFailure(Exception):
    """Soft failure signal: code didn't crash, but results are invalid.

    The orchestrator catches this, triggers LLM diagnosis prose, and transitions
    the state machine to ``AWAITING_FEEDBACK``.
    """

    def __init__(self, reason: str, ctx: dict | None = None):
        self.reason = reason
        self.ctx = ctx or {}
        super().__init__(reason)


class ChatPlan(BaseModel):
    """Base plan schema — all business plan schemas must inherit this.

    Business clients add their own plan-specific fields.
    """

    summary: str = Field(
        ...,
        description="Human-readable summary of the plan: which rows/sections "
                    "to keep/discard, estimated data volume, applied rules.",
    )


@runtime_checkable
class ChatHandler(Protocol):
    """Protocol that each business client must implement.

    Attributes:
        name: Unique identifier, e.g. ``"xpeng-zq"``. Used for registry lookup.
        PlanSchema: ``ChatPlan`` subclass for instructor structured output.
        plan_prompt: System prompt for plan generation.
        diagnose_prompt: System prompt for failure diagnosis.

    Methods:
        intro_message: Return a human-readable preview shown immediately to the user.
        build_intent: Optional structured intent preset (file metadata, field mappings).
        build_skeleton: Convert raw bytes into a compact text representation for the LLM.
        execute: Run the plan on raw data. Raise ``BusinessFailure`` on soft error.
    """

    name: str
    PlanSchema: type[ChatPlan]
    plan_prompt: str
    diagnose_prompt: str

    def intro_message(self, file_name: str) -> str:
        """Return an immediate preview message (no LLM call).

        Describes what rules will be applied, what data will be extracted.
        Use first-person ("I"), refer to the user as ("you").
        """
        ...

    def build_intent(self, file_name: str) -> BaseModel | None:
        """Return structured intent preset (file metadata, field mappings, etc.).

        Optional — return None to skip the intent preview step.
        No LLM call — use filename regex + hardcoded rules.
        """
        return None

    def build_skeleton(self, raw: bytes, *, sheet: int | str = 1) -> str:
        """Convert raw file bytes into a compact text skeleton for the LLM."""
        ...

    async def execute(self, plan: ChatPlan, raw: bytes, *, sheet: int | str = 1) -> list[BaseModel]:
        """Execute the plan on raw data. Raise BusinessFailure on soft error."""
        ...

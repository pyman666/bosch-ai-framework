"""Chat session state machine — 5-state FSM for human-in-the-loop AI workflows.

```
stateDiagram-v2
    [*] --> INTENT_PREVIEW: POST /chat (file uploaded)
    INTENT_PREVIEW --> PLANNING: user clicks "start"
    PLANNING --> AWAITING_CONFIRM: LLM plan generated
    PLANNING --> AWAITING_FEEDBACK: LLM call failed / error
    AWAITING_CONFIRM --> DONE: user confirms + execute succeeds
    AWAITING_CONFIRM --> AWAITING_FEEDBACK: user confirms + BusinessFailure
    AWAITING_CONFIRM --> PLANNING: user sends feedback
    AWAITING_FEEDBACK --> PLANNING: user sends feedback
    DONE --> PLANNING: user re-plans
    DONE --> [*]
```

Each state transition is validated against ``ALLOWED_ENTRIES`` before execution.
"""

from enum import Enum


class SessionState(str, Enum):
    """Chat session states."""

    INTENT_PREVIEW = "intent_preview"
    """File received, preview intent shown. Waiting for user to start."""

    PLANNING = "planning"
    """LLM is generating a plan."""

    AWAITING_CONFIRM = "awaiting_confirm"
    """Plan ready. Waiting for user confirmation or feedback."""

    DONE = "done"
    """Execution succeeded."""

    AWAITING_FEEDBACK = "awaiting_feedback"
    """LLM wrote diagnosis (or plan failed). Waiting for user feedback."""


# Allowed entry states for each operation. Operations should validate against
# this table before proceeding.
ALLOWED_ENTRIES: dict[str, set[SessionState]] = {
    "start_planning": {SessionState.INTENT_PREVIEW},
    "confirm": {SessionState.AWAITING_CONFIRM},
    "feedback": {SessionState.AWAITING_CONFIRM, SessionState.AWAITING_FEEDBACK, SessionState.DONE},
}

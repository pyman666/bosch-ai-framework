"""Tasks module — lightweight async task tracking.

Provides:
    - ``TaskStatus``: processing/success/error enum
    - ``TaskID``, ``PhaseState``, ``TaskState``, ``TaskResult``: Pydantic models
    - ``create_task()``: schedule via FastAPI BackgroundTasks
    - ``get_task()``: poll task by id and phase
    - ``set_phase()``: update phase status/result
    - ``cleanup_expired_tasks()``: TTL-based cleanup
"""

from bosch_ai_framework.tasks.manager import (
    PhaseState,
    TaskID,
    TaskResult,
    TaskState,
    TaskStatus,
    cleanup_expired_tasks,
    create_task,
    get_task,
    set_phase,
)

__all__ = [
    "TaskStatus",
    "TaskID",
    "PhaseState",
    "TaskState",
    "TaskResult",
    "create_task",
    "get_task",
    "set_phase",
    "cleanup_expired_tasks",
]

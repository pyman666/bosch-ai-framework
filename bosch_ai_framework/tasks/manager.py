"""Lightweight async task table + scheduling.

Usage::

    from fastapi import BackgroundTasks
    from bosch_ai_framework.tasks import create_task, get_task, set_phase, TaskStatus

    # In a pipeline, update phase status
    set_phase(task_id, "parse", status=TaskStatus.processing)
    set_phase(task_id, "parse", status=TaskStatus.success, result=rows)

    # In a route
    task = await create_task(background_tasks, my_pipeline, arg1, arg2)
    result = await get_task(task.task_id, "parse")

Note: single-process in-memory store. For multi-worker deployments, use Redis.
"""

from __future__ import annotations

import logging
import os
from enum import Enum
from time import monotonic
from typing import Any, Generic, TypeVar
from uuid import uuid4

from fastapi import BackgroundTasks
from pydantic import BaseModel, Field

T = TypeVar("T")
logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    processing = "processing"
    success = "success"
    error = "error"


class TaskID(BaseModel):
    task_id: str


class PhaseState(BaseModel):
    """State of a single phase."""
    status: TaskStatus | None = None
    result: Any = None
    message: Any = None
    progress: str | None = None  # e.g. "500/10000"


class TaskState(BaseModel):
    phases: dict[str, PhaseState] = Field(default_factory=dict)
    created_at: float = Field(default_factory=monotonic)


class TaskResult(BaseModel, Generic[T]):
    status: TaskStatus | None = None
    result: T | None = None
    message: Any = None
    progress: str | None = None


# Single-process in-memory task table.
TASKS: dict[str, TaskState] = {}
TASK_TTL_SECONDS = 3600  # 1 hour


def _warn_multi_worker() -> None:
    """Warn if gunicorn is configured with multiple workers."""
    workers_env = os.environ.get("WEB_CONCURRENCY") or os.environ.get("GUNICORN_WORKERS")
    if not workers_env:
        return
    try:
        workers = int(workers_env)
    except ValueError:
        return
    if workers > 1:
        logger.warning(
            "Detected workers=%s — task state lives in single-process memory. "
            "Cross-worker GET will miss tasks. Use Redis for production, "
            "or set workers=1.",
            workers_env,
        )


_warn_multi_worker()


def set_phase(
    task_id: str,
    phase: str,
    *,
    status: TaskStatus,
    result: Any = None,
    message: Any = None,
    progress: str | None = None,
) -> None:
    """Update a phase's status/result. Silently no-ops if task not found."""
    task = TASKS.get(task_id)
    if task is None:
        return
    state = task.phases.setdefault(phase, PhaseState())
    state.status = status
    if result is not None:
        state.result = result
    if message is not None:
        state.message = message
    if progress is not None:
        state.progress = progress


async def create_task(
    tasks: BackgroundTasks,
    fn,
    *args,
    **kwargs,
) -> TaskID:
    """Schedule ``fn(task_id, *args, **kwargs)`` via FastAPI BackgroundTasks.

    The function ``fn`` should call ``set_phase(task_id, ...)`` to report progress.
    Callers (e.g. Java BFF) poll ``GET .../tasks/{task_id}`` for results.
    """
    task_id = uuid4().hex
    TASKS[task_id] = TaskState()
    tasks.add_task(fn, task_id, *args, **kwargs)
    return TaskID(task_id=task_id)


async def get_task(task_id: str, phase: str) -> TaskResult:
    """Poll a task by task_id and phase name."""
    task = TASKS.get(task_id)
    if task is None:
        return TaskResult(status=TaskStatus.error, message="Task not found (wrong id or expired)")

    state = task.phases.get(phase)
    if state is None:
        return TaskResult(status=None)

    msg = state.message if state.status == TaskStatus.error else None
    return TaskResult(
        status=state.status,
        result=state.result,
        message=msg,
        progress=state.progress,
    )


async def cleanup_expired_tasks() -> None:
    """Clean up completed tasks older than TTL. Call from lifespan."""
    now = monotonic()
    expired = [
        tid for tid, state in TASKS.items()
        if all(
            p.status in (TaskStatus.success, TaskStatus.error)
            for p in state.phases.values()
        ) and (now - state.created_at) > TASK_TTL_SECONDS
    ]
    for tid in expired:
        del TASKS[tid]
    if expired:
        logger.info("Cleaned up %d expired tasks", len(expired))

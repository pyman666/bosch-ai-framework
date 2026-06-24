"""通用的轻量级异步任务表 + 调度.

用法 (业务方)::

    from fastapi import BackgroundTasks
    from infra.tasks import create_task, get_task, set_phase, TaskStatus

    # 1. 在 pipeline 里更新 phase 状态
    set_phase(task_id, "parse", status=TaskStatus.processing)
    ...
    set_phase(task_id, "parse", status=TaskStatus.success, result=rows)

    # 2. 在 route 里挂任务
    return await create_task(tasks, my_pipeline, ...args)
    return await get_task(task_id, "parse")

多 worker 部署时任务表不共享, 见 ``_warn_multi_worker``.
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
    """单个 phase 的状态."""
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


# ---------------------------------------------------------------------------
# 单进程内存任务表. 单线程 asyncio 下 dict 操作天然原子, 不需要加锁.
# 跨 worker / 跨进程访问任务请改用 Redis 等外部存储 (见下方 _warn_multi_worker).
# ---------------------------------------------------------------------------
TASKS: dict[str, TaskState] = {}

# 已完成任务保留时间 (秒), 超时自动清理
TASK_TTL_SECONDS = 3600  # 1 小时


def _warn_multi_worker() -> None:
    """gunicorn 多 worker 下任务表不共享, POST/GET 会落到不同进程导致 task not found."""
    workers_env = os.environ.get("WEB_CONCURRENCY") or os.environ.get("GUNICORN_WORKERS")
    if not workers_env:
        return
    try:
        workers = int(workers_env)
    except ValueError:
        return
    if workers > 1:
        logger.warning(
            "检测到 workers=%s, 但任务状态存于单进程内存中, "
            "跨 worker 的 GET 将查不到任务. 生产环境请改用 Redis 等外部存储, "
            "或将 workers 设为 1.",
            workers_env,
        )


_warn_multi_worker()


# ---------------------------------------------------------------------------
# task ops
# ---------------------------------------------------------------------------

def set_phase(
    task_id: str,
    phase: str,
    *,
    status: TaskStatus,
    result: Any = None,
    message: Any = None,
    progress: str | None = None,
) -> None:
    """更新某个 phase 的状态/结果. 任务不存在时静默跳过."""
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
    """在 BackgroundTasks 里挂一个 ``fn(task_id, *args, **kwargs)`` 异步执行, 返回 task_id.

    调用方 ``fn`` 内部用 ``set_phase(task_id, ...)`` 上报进度. M2M 调用方 (Java 后端) 拿到
    ``task_id`` 后用 ``GET .../tasks/{task_id}`` 轮询取结果.
    """
    task_id = uuid4().hex
    TASKS[task_id] = TaskState()
    tasks.add_task(fn, task_id, *args, **kwargs)
    return TaskID(task_id=task_id)


async def get_task(task_id: str, phase: str) -> TaskResult:
    """按 phase 取一个任务的结果. task 不存在 -> error; phase 没启动 -> status=None."""
    task = TASKS.get(task_id)
    if task is None:
        return TaskResult(status=TaskStatus.error, message="任务不存在 (task_id 错误或已过期)")

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
    """清理已过期的已完成任务, 防止内存泄漏. 由 lifespan 定期调用."""
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

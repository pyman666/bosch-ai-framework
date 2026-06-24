"""TaskBackend — 任务存储后端抽象.

提供:

- ``TaskBackend(ABC)`` — 接口: submit / cancel / status / set_phase / cleanup
- ``MemoryTaskBackend`` — 单进程内存实现（当前默认）
- ``DEFAULT_BACKEND`` — 全局默认后端实例
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks

from infra.task.types import PhaseState, TaskID, TaskResult, TaskState, TaskStatus

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------


class TaskBackend(ABC):
    """任务存储后端接口.

    当前实现: MemoryTaskBackend (单进程内存表)
    未来实现: RedisTaskBackend, CeleryTaskBackend
    """

    @abstractmethod
    def submit(self, tasks: BackgroundTasks, fn, *args, **kwargs) -> TaskID:
        """提交异步任务，返回 task_id."""

    @abstractmethod
    def set_phase(
        self,
        task_id: str,
        phase: str,
        *,
        status: TaskStatus,
        result: Any = None,
        message: Any = None,
        progress: str | None = None,
    ) -> None:
        """更新某个 phase 的状态."""

    @abstractmethod
    def get(self, task_id: str, phase: str) -> TaskResult:
        """按 phase 查询任务结果."""

    @abstractmethod
    def cancel(self, task_id: str) -> None:
        """取消任务 (标记为 error)."""

    @abstractmethod
    def cleanup(self) -> None:
        """清理过期的已完成任务."""


# ---------------------------------------------------------------------------
# MemoryTaskBackend
# ---------------------------------------------------------------------------


class MemoryTaskBackend(TaskBackend):
    """单进程内存任务表.

    单线程 asyncio 下 dict 操作天然原子，不需要锁。
    多 worker 部署时不共享 — 生产环境请用 RedisTaskBackend (未来).
    """

    TASK_TTL_SECONDS = 3600  # 1 小时

    def __init__(self) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._warn_multi_worker()

    # -------------------------------------------------------------------
    # Submit
    # -------------------------------------------------------------------

    def submit(self, tasks: BackgroundTasks, fn, *args, **kwargs) -> TaskID:
        """在 BackgroundTasks 里挂 ``fn(task_id, *args, **kwargs)`` 异步执行."""
        task_id: TaskID
        task_id = TaskID(task_id=uuid4().hex)
        self._tasks[task_id.task_id] = TaskState()
        tasks.add_task(fn, task_id.task_id, *args, **kwargs)
        return task_id

    # -------------------------------------------------------------------
    # Phase
    # -------------------------------------------------------------------

    def set_phase(
        self,
        task_id: str,
        phase: str,
        *,
        status: TaskStatus,
        result: Any = None,
        message: Any = None,
        progress: str | None = None,
    ) -> None:
        """更新某个 phase 的状态/结果. 任务不存在时静默跳过."""
        task = self._tasks.get(task_id)
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

    # -------------------------------------------------------------------
    # Get
    # -------------------------------------------------------------------

    def get(self, task_id: str, phase: str) -> TaskResult:
        """按 phase 取任务结果."""
        task = self._tasks.get(task_id)
        if task is None:
            return TaskResult(status=TaskStatus.error, message="任务不存在 (task_id 错误或已过期)")

        state = task.phases.get(phase)
        if state is None:
            return TaskResult(status=None)

        msg = state.message if state.status == TaskStatus.error else None
        return TaskResult(status=state.status, result=state.result, message=msg, progress=state.progress)

    # -------------------------------------------------------------------
    # Cancel
    # -------------------------------------------------------------------

    def cancel(self, task_id: str) -> None:
        """取消任务 (所有 phase 标记为 error)."""
        task = self._tasks.get(task_id)
        if task is None:
            return
        for phase in task.phases.values():
            if phase.status == TaskStatus.processing:
                phase.status = TaskStatus.error
                phase.message = "Cancelled"

    # -------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------

    def cleanup(self) -> None:
        """清理已过期的已完成任务."""
        now = monotonic()
        expired = [
            tid
            for tid, state in self._tasks.items()
            if all(p.status in (TaskStatus.success, TaskStatus.error) for p in state.phases.values())
            and (now - state.created_at) > self.TASK_TTL_SECONDS
        ]
        for tid in expired:
            del self._tasks[tid]
        if expired:
            log.info("Cleaned up %d expired tasks", len(expired))

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    @staticmethod
    def _warn_multi_worker() -> None:
        """gunicorn 多 worker 下任务表不共享."""
        workers_env = os.environ.get("WEB_CONCURRENCY") or os.environ.get("GUNICORN_WORKERS")
        if not workers_env:
            return
        try:
            workers = int(workers_env)
        except ValueError:
            return
        if workers > 1:
            log.warning(
                "workers=%s, 任务状态存于单进程内存，多 worker 下 GET 可能查不到任务。"
                "生产环境请用 RedisTaskBackend (未来实现).",
                workers_env,
            )


# ---------------------------------------------------------------------------
# Default backend
# ---------------------------------------------------------------------------

DEFAULT_BACKEND = MemoryTaskBackend()

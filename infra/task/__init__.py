"""Task 管理 — 异步任务提交 + phase 状态追踪.

用法::

    from infra.task import create_task, get_task, set_phase, TaskStatus

    # 提交任务
    task_id = await create_task(background_tasks, my_pipeline, arg1, arg2)

    # pipeline 内更新 phase
    set_phase(task_id.task_id, "parse", status=TaskStatus.processing)
    set_phase(task_id.task_id, "parse", status=TaskStatus.success, result=data)

    # 查询结果
    result = await get_task(task_id.task_id, "parse")

后端可替换:
    from infra.task.backend import DEFAULT_BACKEND, RedisTaskBackend
    # 未来: DEFAULT_BACKEND = RedisTaskBackend(redis_url)
"""

from infra.task.backend import DEFAULT_BACKEND
from infra.task.types import PhaseState, TaskID, TaskResult, TaskState, TaskStatus


# ---------------------------------------------------------------------------
# Convenience functions — delegate to default backend
# ---------------------------------------------------------------------------


async def create_task(tasks, fn, *args, **kwargs) -> TaskID:
    """提交异步任务. ``fn(task_id, *args, **kwargs)`` 在后台执行."""
    return DEFAULT_BACKEND.submit(tasks, fn, *args, **kwargs)


def set_phase(
    task_id: str,
    phase: str,
    *,
    status: TaskStatus,
    result=None,
    message=None,
    progress: str | None = None,
) -> None:
    """更新某个 phase 的状态."""
    DEFAULT_BACKEND.set_phase(task_id, phase, status=status, result=result, message=message, progress=progress)


async def get_task(task_id: str, phase: str) -> TaskResult:
    """按 phase 查询任务结果."""
    return DEFAULT_BACKEND.get(task_id, phase)


async def cleanup_expired_tasks() -> None:
    """清理过期任务 (lifespan 定期调用)."""
    DEFAULT_BACKEND.cleanup()


__all__ = [
    "PhaseState",
    "TaskID",
    "TaskResult",
    "TaskState",
    "TaskStatus",
    "cleanup_expired_tasks",
    "create_task",
    "get_task",
    "set_phase",
]

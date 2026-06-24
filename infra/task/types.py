"""Task 类型定义."""

from __future__ import annotations

from enum import Enum
from time import monotonic
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


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

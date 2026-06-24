"""Executor 接口 — Skill/DSL/沙箱 执行抽象.

每种执行模式 (DSL / Python 沙箱 / Preset / 远程 API) 实现一个 Executor，
Agent 按需组合。

用法::

    from infra.agent.executor import Executor, ExecutionResult

    class DSLExecutor(Executor):
        async def execute(self, input: dict, **opts) -> ExecutionResult:
            ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionResult:
    """执行结果."""

    success: bool
    data: Any = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Executor(ABC):
    """Skill 执行器接口.

    子类实现一种执行模式:
        - DSLExecutor — 领域特定语言
        - SandboxExecutor — Python 沙箱
        - PresetExecutor — 预设函数直接调用
        - RemoteExecutor — 远程 API (TimesFM, Chronos, etc.)
    """

    @abstractmethod
    async def execute(self, input: dict[str, Any], **options: Any) -> ExecutionResult:
        """执行并返回结果."""

    @property
    @abstractmethod
    def name(self) -> str:
        """执行器名称 (用于日志/调试)."""

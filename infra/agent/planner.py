"""Planner 接口 — 任务规划抽象.

Planner 将用户意图拆解为可执行的步骤序列 (plan → execute → verify)。
不同规划策略 (ReAct / Plan-Execute / Tree-of-Thought) 各自实现。

用法::

    from infra.agent.planner import Planner, Plan, Step

    class ReActPlanner(Planner):
        async def plan(self, goal: str, context: dict) -> Plan:
            ...
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    error = "error"


@dataclass
class Step:
    """单个执行步骤."""

    name: str
    action: str  # tool name / skill name / function name
    args: dict[str, Any] = field(default_factory=dict)
    status: StepStatus = StepStatus.pending
    result: Any = None
    error: str = ""


@dataclass
class Plan:
    """执行计划 — 有序步骤列表."""

    goal: str
    steps: list[Step] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class Planner(ABC):
    """规划器接口.

    输入用户意图 + 上下文 → 输出可执行步骤序列。
    """

    @abstractmethod
    async def plan(self, goal: str, context: dict[str, Any]) -> Plan:
        """制定执行计划."""

    @abstractmethod
    async def replan(self, plan: Plan, failed_step: Step, error: str) -> Plan:
        """步骤失败后重新规划."""

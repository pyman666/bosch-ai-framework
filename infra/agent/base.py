"""BaseAgent — Agent 基类.

所有业务 Agent 继承此类，声明式注入 skills / tools / system_prompt，
Agent Loop 由框架提供。

用法::

    from infra.agent import BaseAgent, AgentLoopConfig

    class ForecastAgent(BaseAgent):
        system_prompt = "..."
        tools: ToolRegistry = ...
        skills: SkillRegistry = ...
        config = AgentLoopConfig(max_turns=10)

        async def pre_process(self, messages): ...
        async def post_process(self, result): ...

    agent = ForecastAgent()
    result = await agent.run(messages=[...])
"""

from __future__ import annotations

import logging
from abc import ABC
from typing import Any, AsyncIterator

from infra.agent.loop import AgentLoop, AgentLoopConfig
from infra.agent.tool import ToolRegistry
from infra.skill import SkillRegistry

log = logging.getLogger(__name__)


class BaseAgent(ABC):
    """Agent 基类 — 声明式定义，框架执行。

    Subclass 只需声明:
        system_prompt: str
        tools: ToolRegistry | None
        skills: SkillRegistry | None
        config: AgentLoopConfig

    然后调用:
        result = await agent.run(messages=[...])
        async for event in agent.run_stream(messages=[...]): ...
    """

    system_prompt: str = ""
    app_name: str = ""
    tools: ToolRegistry | None = None
    skills: SkillRegistry | None = None
    config: AgentLoopConfig = AgentLoopConfig()

    # -------------------------------------------------------------------
    # Lifecycle (subclass 可覆盖)
    # -------------------------------------------------------------------

    async def on_start(self) -> None:
        """Agent 初始化 — 加载模型、预热缓存等。子类可覆盖。"""

    async def on_stop(self) -> None:
        """Agent 清理 — 释放资源。子类可覆盖。"""

    async def pre_process(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """消息预处理 — 注入上下文、裁剪历史等。子类可覆盖。"""
        return messages

    async def post_process(self, result: dict[str, Any]) -> dict[str, Any]:
        """结果后处理 — 格式化输出、保存记忆等。子类可覆盖。"""
        return result

    # -------------------------------------------------------------------
    # Run (non-streaming)
    # -------------------------------------------------------------------

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """执行 Agent 循环（非流式）。

        Returns:
            {"content": str, "messages": list, "tool_calls": list}
        """
        await self.on_start()
        try:
            messages = await self.pre_process(messages)
            loop = self._build_loop()
            result = await loop.run(messages, model=model)
            return await self.post_process(result)
        finally:
            await self.on_stop()

    # -------------------------------------------------------------------
    # Run (streaming)
    # -------------------------------------------------------------------

    async def run_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """执行 Agent 循环（流式），yield SSE 事件。

        Yields:
            {"type": "delta", "content": str}
            {"type": "tool_call", ...}
            {"type": "tool_result", ...}
            {"type": "done", "content": str, "messages": list}
        """
        await self.on_start()
        try:
            messages = await self.pre_process(messages)
            loop = self._build_loop()
            async for event in loop.run_stream(messages, model=model):
                yield event
        finally:
            await self.on_stop()

    # -------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------

    def _build_loop(self) -> AgentLoop:
        return AgentLoop(
            registry=self.tools or ToolRegistry(),
            system_prompt=self.system_prompt,
            config=self.config,
            app_name=self.app_name,
        )

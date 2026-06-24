"""Agent 框架 — Tool 注册 + Agent 循环.

用法::

    from infra.agent import ToolRegistry, AgentLoop, AgentLoopConfig

    registry = ToolRegistry()

    @registry.register("search", {...})
    def search(args: dict) -> str:
        return json.dumps({"results": [...]})

    loop = AgentLoop(registry=registry, system_prompt="...")
    result = await loop.run(messages=[...])
"""

from infra.agent.loop import AgentLoop, AgentLoopConfig
from infra.agent.tool import Tool, ToolHandler, ToolRegistry

__all__ = [
    "AgentLoop",
    "AgentLoopConfig",
    "Tool",
    "ToolHandler",
    "ToolRegistry",
]

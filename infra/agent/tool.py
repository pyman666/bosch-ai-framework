"""Tool 注册与执行框架.

提供:

- ``Tool`` dataclass — 工具定义
- ``ToolRegistry`` — 工具注册表，收集 schema + handler
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ToolHandler = Callable[[dict[str, Any]], str | Awaitable[str]]


class ToolHandlerProtocol(Protocol):
    """工具 handler 协议 — 同步或异步函数."""

    def __call__(self, args: dict[str, Any]) -> str | Awaitable[str]:
        ...


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """单个工具的完整定义."""

    name: str
    definition: dict[str, Any]
    handler: ToolHandler
    is_async: bool = False


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """工具注册表 — 收集工具定义和 handler.

    用法::

        registry = ToolRegistry()

        @registry.register("my_tool", {
            "type": "function",
            "function": {
                "name": "my_tool",
                "description": "...",
                "parameters": {...},
            },
        })
        def my_tool_handler(args: dict) -> str:
            return json.dumps({"result": "ok"})
    """

    def __init__(self) -> None:
        self._registry: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        definition: dict[str, Any],
        *,
        is_async: bool = False,
    ) -> Callable[[ToolHandler], ToolHandler]:
        """装饰器：注册工具 handler 并绑定 schema 定义."""

        def decorator(fn: ToolHandler) -> ToolHandler:
            self._registry[name] = Tool(
                name=name,
                definition=definition,
                handler=fn,
                is_async=is_async,
            )
            return fn

        return decorator

    def get_definitions(self) -> list[dict[str, Any]]:
        """返回所有工具的 schema 定义列表 (供 LLM tools 参数用)."""
        return [t.definition for t in self._registry.values()]

    def get_tool(self, name: str) -> Tool | None:
        """按名称获取工具."""
        return self._registry.get(name)

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        """执行指定工具.

        Returns:
            工具执行结果的 JSON 字符串
        """
        tool = self._registry.get(name)
        if not tool:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            if tool.is_async:
                return await tool.handler(args)
            return tool.handler(args)
        except Exception as e:
            log.exception(f"Tool {name} failed: {e}")
            return json.dumps({"error": str(e)})

    def __contains__(self, name: str) -> bool:
        return name in self._registry

    def __len__(self) -> int:
        return len(self._registry)

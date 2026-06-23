"""Agent framework — Tool registry.

Provides a decorator-based tool registration system for LLM function calling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], str | Awaitable[str]]


@dataclass
class Tool:
    """Complete definition of a single tool."""
    name: str
    definition: dict[str, Any]
    handler: ToolHandler
    is_async: bool = False


class ToolRegistry:
    """Tool registry — collects tool definitions and handlers.

    Usage::

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
        """Decorator: register a tool handler with its schema definition."""

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
        """Return all tool schema definitions (for LLM tools parameter)."""
        return [t.definition for t in self._registry.values()]

    def get_tool(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._registry.get(name)

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        """Execute a tool by name.

        Returns:
            JSON string of the tool's result.
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

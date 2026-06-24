"""通用 Agent 工具调度框架 — Tool Registry + Agent Loop.

提供可复用的工具注册、调度和 Agent 循环基础设施，支持流式/非流式两种模式。

用法::

    from infra.tools import ToolRegistry, AgentLoop

    # 1. 定义工具
    registry = ToolRegistry()

    @registry.register("search", {
        "type": "function",
        "function": {
            "name": "search",
            "description": "搜索...",
            "parameters": {"type": "object", "properties": {...}},
        },
    })
    async def my_search_tool(args: dict) -> str:
        return json.dumps({"results": [...]})

    # 2. 运行 Agent 循环
    loop = AgentLoop(
        registry=registry,
        system_prompt="...",
        max_turns=10,
    )

    # 非流式
    result = await loop.run(messages=[...])

    # 流式
    async for event in loop.run_stream(messages=[...]):
        if event["type"] == "delta":
            print(event["content"])
        elif event["type"] == "tool_call":
            print(f"Tool: {event['name']}")
        elif event["type"] == "done":
            print("Finished")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

from infra.llm import chat, chat_stream

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
# Tool Registry
# ---------------------------------------------------------------------------

@dataclass
class Tool:
    """单个工具的完整定义."""
    name: str
    definition: dict[str, Any]
    handler: ToolHandler
    is_async: bool = False


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

        Args:
            name: 工具名称
            args: 工具参数

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


# ---------------------------------------------------------------------------
# Agent Loop
# ---------------------------------------------------------------------------

@dataclass
class AgentLoopConfig:
    """Agent 循环配置."""
    max_turns: int = 10
    """最大轮数，防止无限循环."""

    max_tool_calls: int = 5
    """单轮最多工具调用次数."""

    model: str | None = None
    """指定模型，默认使用 settings.DEFAULT_MODEL."""


class AgentLoop:
    """通用 Agent 工具调用循环.

    封装了 LLM chat → tool call → execute → 继续 的完整循环逻辑，
    支持流式 (SSE) 和非流式两种模式。

    用法 (非流式)::

        loop = AgentLoop(registry=my_registry, system_prompt="...")
        result = await loop.run(messages=[{"role": "user", "content": "..."}])

    用法 (流式)::

        async for event in loop.run_stream(messages=[...]):
            if event["type"] == "delta":
                print(event["content"])
            elif event["type"] == "tool_call":
                print(f"Calling {event['name']}")
            elif event["type"] == "done":
                print("Finished")
    """

    def __init__(
        self,
        registry: ToolRegistry,
        system_prompt: str = "",
        config: AgentLoopConfig | None = None,
    ) -> None:
        self.registry = registry
        self.system_prompt = system_prompt
        self.config = config or AgentLoopConfig()

    # -----------------------------------------------------------------------
    # Non-streaming
    # -----------------------------------------------------------------------

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """运行 Agent 循环（非流式），返回最终状态.

        Returns:
            {
                "content": str,          # assistant 最终回复
                "messages": list,      # 完整消息历史
                "tool_calls": list,      # 所有工具调用记录
            }
        """
        working_messages = self._build_messages(messages)
        tool_call_records: list[dict[str, Any]] = []
        tool_call_count = 0

        for turn in range(self.config.max_turns):
            response = await chat(
                working_messages,
                tools=self.registry.get_definitions(),
                model=model or self.config.model,
            )

            tool_calls = response.get("tool_calls", [])

            if not tool_calls:
                # 没有 tool call，完成
                working_messages.append({
                    "role": "assistant",
                    "content": response["content"],
                })
                return {
                    "content": response["content"],
                    "messages": working_messages,
                    "tool_calls": tool_call_records,
                }

            # 有 tool call，执行并继续
            working_messages.append({
                "role": "assistant",
                "content": response["content"] or None,
                "tool_calls": [{
                    "id": tc["id"],
                    "type": "function",
                    "function": tc["function"],
                } for tc in tool_calls],
            })

            for tc in tool_calls:
                if tool_call_count >= self.config.max_tool_calls:
                    break
                tool_call_count += 1

                name = tc["function"]["name"]
                args = _safe_parse_args(tc["function"]["arguments"])
                result = await self.registry.execute(name, args)

                tool_call_records.append({
                    "tool_call_id": tc["id"],
                    "name": name,
                    "args": args,
                    "result": result,
                })

                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            if tool_call_count >= self.config.max_tool_calls:
                break

        # 达到上限，强制结束
        final = await chat(working_messages, tools=None, model=model or self.config.model)
        working_messages.append({
            "role": "assistant",
            "content": final["content"],
        })

        return {
            "content": final["content"],
            "messages": working_messages,
            "tool_calls": tool_call_records,
        }

    # -----------------------------------------------------------------------
    # Streaming
    # -----------------------------------------------------------------------

    async def run_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """运行 Agent 循环（流式），yield 事件.

        Yields:
            - ``{"type": "delta", "content": str}`` -- 文本增量
            - ``{"type": "tool_call", "tool_call_id": str, "name": str, "args": dict}`` -- 工具调用
            - ``{"type": "tool_result", "tool_call_id": str, "name": str, "result": str}`` -- 工具结果
            - ``{"type": "done", "content": str, "messages": list}`` -- 完成
        """
        working_messages = self._build_messages(messages)
        tool_call_records: list[dict[str, Any]] = []
        tool_call_count = 0

        for turn in range(self.config.max_turns):
            tool_calls_this_turn: list[dict[str, Any]] = []
            content_acc = ""

            async for event in chat_stream(
                working_messages,
                tools=self.registry.get_definitions(),
                model=model or self.config.model,
            ):
                if "delta" in event:
                    content_acc += event["delta"]
                    yield {"type": "delta", "content": event["delta"]}

                elif "finish" in event:
                    tc = event.get("tool_calls")
                    if tc:
                        tool_calls_this_turn = tc
                    break

            # 没有 tool call
            if not tool_calls_this_turn:
                working_messages.append({
                    "role": "assistant",
                    "content": content_acc,
                })
                yield {
                    "type": "done",
                    "content": content_acc,
                    "messages": working_messages,
                    "tool_calls": tool_call_records,
                }
                return

            # 有 tool call，执行
            working_messages.append({
                "role": "assistant",
                "content": content_acc or None,
                "tool_calls": [{
                    "id": tc["id"],
                    "type": "function",
                    "function": tc["function"],
                } for tc in tool_calls_this_turn],
            })

            for tc in tool_calls_this_turn:
                if tool_call_count >= self.config.max_tool_calls:
                    break
                tool_call_count += 1

                name = tc["function"]["name"]
                args = _safe_parse_args(tc["function"]["arguments"])

                yield {
                    "type": "tool_call",
                    "tool_call_id": tc["id"],
                    "name": name,
                    "args": args,
                }

                result = await self.registry.execute(name, args)

                yield {
                    "type": "tool_result",
                    "tool_call_id": tc["id"],
                    "name": name,
                    "result": result,
                }

                tool_call_records.append({
                    "tool_call_id": tc["id"],
                    "name": name,
                    "args": args,
                    "result": result,
                })

                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            if tool_call_count >= self.config.max_tool_calls:
                break

        # 达到上限
        final = await chat(working_messages, tools=None, model=model or self.config.model)
        working_messages.append({
            "role": "assistant",
            "content": final["content"],
        })
        yield {
            "type": "done",
            "content": final["content"],
            "messages": working_messages,
            "tool_calls": tool_call_records,
        }

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _build_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """构建包含 system prompt 的完整消息列表."""
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}] + list(messages)
        return list(messages)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_parse_args(raw: str | None) -> dict[str, Any]:
    """安全解析 tool arguments JSON."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"Tool arguments JSON 解析失败: {str(raw)[:200]}")
        return {}

"""Agent 工具调用循环.

提供:

- ``AgentLoopConfig`` — 循环配置
- ``AgentLoop`` — 通用 Agent 循环，支持流式/非流式
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

from infra.agent.tool import ToolRegistry
from infra.llm import chat, chat_stream

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
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


# ---------------------------------------------------------------------------
# AgentLoop
# ---------------------------------------------------------------------------


class AgentLoop:
    """通用 Agent 工具调用循环.

    封装 LLM chat → tool call → execute → 继续 的完整循环，支持流式和非流式。

    用法 (非流式)::

        loop = AgentLoop(registry=my_registry, system_prompt="...")
        result = await loop.run(messages=[{"role": "user", "content": "..."}])

    用法 (流式)::

        async for event in loop.run_stream(messages=[...]):
            if event["type"] == "delta":
                print(event["content"])
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

    # -------------------------------------------------------------------
    # Non-streaming
    # -------------------------------------------------------------------

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """运行 Agent 循环（非流式）.

        Returns:
            {"content": str, "messages": list, "tool_calls": list}
        """
        working = self._build_messages(messages)
        records: list[dict[str, Any]] = []
        tc_count = 0

        for _ in range(self.config.max_turns):
            resp = await chat(
                working,
                tools=self.registry.get_definitions(),
                model=model or self.config.model,
            )

            tcs = resp.get("tool_calls", [])

            if not tcs:
                working.append({"role": "assistant", "content": resp["content"]})
                return {"content": resp["content"], "messages": working, "tool_calls": records}

            working.append({
                "role": "assistant",
                "content": resp["content"] or None,
                "tool_calls": [
                    {"id": tc["id"], "type": "function", "function": tc["function"]}
                    for tc in tcs
                ],
            })

            for tc in tcs:
                if tc_count >= self.config.max_tool_calls:
                    break
                tc_count += 1
                name = tc["function"]["name"]
                args = _safe_parse_args(tc["function"]["arguments"])
                result = await self.registry.execute(name, args)

                records.append({"tool_call_id": tc["id"], "name": name, "args": args, "result": result})
                working.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

            if tc_count >= self.config.max_tool_calls:
                break

        final = await chat(working, tools=None, model=model or self.config.model)
        working.append({"role": "assistant", "content": final["content"]})
        return {"content": final["content"], "messages": working, "tool_calls": records}

    # -------------------------------------------------------------------
    # Streaming
    # -------------------------------------------------------------------

    async def run_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """运行 Agent 循环（流式），yield 事件.

        Yields:
            {"type": "delta", "content": str}
            {"type": "tool_call", "tool_call_id": str, "name": str, "args": dict}
            {"type": "tool_result", "tool_call_id": str, "name": str, "result": str}
            {"type": "done", "content": str, "messages": list}
        """
        working = self._build_messages(messages)
        records: list[dict[str, Any]] = []
        tc_count = 0

        for _ in range(self.config.max_turns):
            tcs_this_turn: list[dict[str, Any]] = []
            content_acc = ""

            async for event in chat_stream(
                working,
                tools=self.registry.get_definitions(),
                model=model or self.config.model,
            ):
                if "delta" in event:
                    content_acc += event["delta"]
                    yield {"type": "delta", "content": event["delta"]}
                elif "finish" in event:
                    tc = event.get("tool_calls")
                    if tc:
                        tcs_this_turn = tc
                    break

            if not tcs_this_turn:
                working.append({"role": "assistant", "content": content_acc})
                yield {"type": "done", "content": content_acc, "messages": working, "tool_calls": records}
                return

            working.append({
                "role": "assistant",
                "content": content_acc or None,
                "tool_calls": [
                    {"id": tc["id"], "type": "function", "function": tc["function"]}
                    for tc in tcs_this_turn
                ],
            })

            for tc in tcs_this_turn:
                if tc_count >= self.config.max_tool_calls:
                    break
                tc_count += 1
                name = tc["function"]["name"]
                args = _safe_parse_args(tc["function"]["arguments"])

                yield {"type": "tool_call", "tool_call_id": tc["id"], "name": name, "args": args}
                result = await self.registry.execute(name, args)
                yield {"type": "tool_result", "tool_call_id": tc["id"], "name": name, "result": result}

                records.append({"tool_call_id": tc["id"], "name": name, "args": args, "result": result})
                working.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

            if tc_count >= self.config.max_tool_calls:
                break

        final = await chat(working, tools=None, model=model or self.config.model)
        working.append({"role": "assistant", "content": final["content"]})
        yield {"type": "done", "content": final["content"], "messages": working, "tool_calls": records}

    # -------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------

    def _build_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
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

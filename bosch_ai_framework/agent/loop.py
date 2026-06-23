"""Agent framework — Tool-calling loop.

Provides a reusable agent loop that handles LLM → tool call → execute → continue,
with both streaming (SSE) and non-streaming modes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator

from bosch_ai_framework.agent.registry import ToolRegistry
from bosch_ai_framework.llm.router import chat, chat_stream

log = logging.getLogger(__name__)


@dataclass
class AgentLoopConfig:
    """Agent loop configuration."""

    max_turns: int = 10
    """Maximum number of LLM turns to prevent infinite loops."""

    max_tool_calls: int = 5
    """Maximum tool calls across all turns."""

    model: str | None = None
    """Model override (default: config.DEFAULT_MODEL)."""


class AgentLoop:
    """Generic agent tool-calling loop.

    Encapsulates the LLM chat → tool call → execute → continue cycle,
    with both streaming and non-streaming modes.

    Usage (non-streaming)::

        loop = AgentLoop(registry=my_registry, system_prompt="...")
        result = await loop.run(messages=[{"role": "user", "content": "..."}])

    Usage (streaming)::

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

    # -- Non-streaming -------------------------------------------------------

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Run agent loop (non-streaming), returns final state.

        Returns:
            {"content": str, "messages": list, "tool_calls": list}
        """
        working_messages = self._build_messages(messages)
        tool_call_records: list[dict[str, Any]] = []
        tool_call_count = 0

        for _turn in range(self.config.max_turns):
            response = await chat(
                working_messages,
                tools=self.registry.get_definitions(),
                model=model or self.config.model,
            )

            tool_calls = response.get("tool_calls", [])

            if not tool_calls:
                working_messages.append({
                    "role": "assistant",
                    "content": response["content"],
                })
                return {
                    "content": response["content"],
                    "messages": working_messages,
                    "tool_calls": tool_call_records,
                }

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

        # Hit turn limit — force final response
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

    # -- Streaming -----------------------------------------------------------

    async def run_stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run agent loop (streaming), yields events.

        Yields:
            ``{"type": "delta", "content": str}``
            ``{"type": "tool_call", "tool_call_id": str, "name": str, "args": dict}``
            ``{"type": "tool_result", "tool_call_id": str, "name": str, "result": str}``
            ``{"type": "done", "content": str, "messages": list}``
        """
        working_messages = self._build_messages(messages)
        tool_call_records: list[dict[str, Any]] = []
        tool_call_count = 0

        for _turn in range(self.config.max_turns):
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

    # -- Helpers -------------------------------------------------------------

    def _build_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build full message list including system prompt."""
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}] + list(messages)
        return list(messages)


def _safe_parse_args(raw: str | None) -> dict[str, Any]:
    """Safely parse tool arguments JSON."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"Tool arguments JSON parse failed: {str(raw)[:200]}")
        return {}

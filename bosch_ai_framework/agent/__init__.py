"""Agent module — Tool registry + agent loop.

Provides:
    - ``ToolRegistry``: decorator-based tool registration
    - ``Tool``: tool definition dataclass
    - ``AgentLoop``: multi-turn tool-calling loop (streaming + non-streaming)
    - ``AgentLoopConfig``: loop configuration
"""

from bosch_ai_framework.agent.registry import (
    Tool,
    ToolRegistry,
)
from bosch_ai_framework.agent.loop import (
    AgentLoop,
    AgentLoopConfig,
)

__all__ = [
    "Tool",
    "ToolRegistry",
    "AgentLoop",
    "AgentLoopConfig",
]

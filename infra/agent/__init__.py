"""Agent 框架.

用法::

    from infra.agent import BaseAgent, ToolRegistry, AgentLoop
    from infra.skill import SkillRegistry

    class MyAgent(BaseAgent):
        system_prompt = "..."
        tools = ToolRegistry()
        skills = SkillRegistry()

    agent = MyAgent()
    result = await agent.run(messages=[...])
    async for event in agent.run_stream(messages=[...]):
        ...
"""

from infra.agent.base import BaseAgent  # noqa: F401
from infra.agent.executor import ExecutionResult, Executor  # noqa: F401
from infra.agent.loop import AgentLoop, AgentLoopConfig  # noqa: F401
from infra.agent.memory import AgentMemory  # noqa: F401
from infra.agent.planner import Plan, Planner, Step, StepStatus  # noqa: F401
from infra.agent.tool import Tool, ToolHandler, ToolRegistry  # noqa: F401


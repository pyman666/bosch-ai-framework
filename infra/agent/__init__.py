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

from infra.agent.base import BaseAgent
from infra.agent.executor import ExecutionResult, Executor
from infra.agent.loop import AgentLoop, AgentLoopConfig
from infra.agent.memory import AgentMemory
from infra.agent.planner import Plan, Planner, Step, StepStatus
from infra.agent.tool import Tool, ToolHandler, ToolRegistry


"""infra — AI 项目公共基础设施包.

用法::

    from infra.llm import chat, stream, get_router
    from infra.agent import ToolRegistry, AgentLoop
    from infra.skill import Skill, SkillRegistry
    from infra.task import create_task, get_task, set_phase, TaskStatus
    from infra.auth import require_auth
    from infra.settings import load_config
"""

__version__ = "0.1.0"

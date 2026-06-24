"""infra — LLM 项目公共基础设施包.

包含所有新项目可复用的底层能力：
- llm:        LiteLLM Router 封装 + chat/chat_stream
- settings:   模型配置加载 (YAML -> MODEL_LIST)
- auth:       Basic / XSUAA 双轨鉴权 FastAPI dependency
- tasks:      异步任务调度 + phase 状态管理
- logs:   Gunicorn JSON logging + QueueHandler 配置
- utils:      通用工具函数
- tools:      通用 Agent 工具调度框架 (ToolRegistry + AgentLoop)

用法::

    from infra.llm import chat, chat_stream, get_router
    from infra.auth import require_auth
    from infra.tasks import create_task, get_task, set_phase, TaskStatus
    from infra.settings import DEFAULT_MODEL, MODEL_LIST
    from infra.tools import ToolRegistry, AgentLoop
"""

__version__ = "0.1.0"

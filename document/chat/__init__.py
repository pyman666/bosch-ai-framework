"""Chat = LLM-in-the-loop **skill orchestration state machine** (人在环路).

本质是一台事件驱动的状态机, 协调 (orchestrate) 一组业务侧实现的 skill 跟 LLM 的
planner / diagnoser, 让 "解析复杂 Excel" 这种没法一遍跑通的任务变成 "跑 → 给业务方
看 plan → 业务方反馈 / 确认 → 跑 → 失败再 LLM 出诊断 → 再 plan ..." 的闭环.

跟 ``apdfi/pdf/pipeline/`` (linear workflow) 是**两种范式**:

    | 范式            | PDF (workflow)               | Chat (skill orchestration)        |
    | --------------- | ---------------------------- | --------------------------------- |
    | 步骤            | 静态 DAG, 加 flag 控制开关   | 运行时由 LLM 决策 + 用户反馈驱动  |
    | LLM 角色        | transform 函数 (抽字段)      | decision maker (规划 / 诊断)      |
    | 加新业务客户    | 新 schema, pipeline 不动     | 新 skill 实现 + ``register(...)`` |

状态机详见 ``state.SessionState``: ``intent_preview → planning → awaiting_confirm →
done`` (主路径) + ``done / awaiting_feedback ↔ planning`` (反馈回路).

业务接入 = **写一组 skill 实现**, 而不是 "写聊天消息处理器":

    1. 定义 ``MyPlan(ChatPlan)``, ``MyRow(BaseModel)``.
    2. 实现 ``MyHandler`` 满足 ``ChatHandler`` 协议 -- 这是 **skill 接口**, 4 个回调:
        - ``plan_prompt`` / ``diagnose_prompt`` (LLM 系统消息);
        - ``build_skeleton(raw)`` (skeleton 渲染 skill, 给 planner LLM 看的紧凑文本);
        - ``execute(plan, raw)`` (plan -> rows skill, 业务核心);
       可选: ``intro_message(file_name)`` / ``build_intent(file_name)`` 上传瞬间渲染.
    3. ``register(MyHandler())`` (module top-level), 把 skill 注册进 orchestrator.
    4. 在业务路由文件里写**类型化的**入口 endpoint:
        - ``POST /<biz>/chat``                         -> ``ops.start(handler_name="<biz>", ...)``
        - ``POST /<biz>/chat/{chat_id}/start``         -> ``ops.start_planning(...)``
        - ``GET  /<biz>/chat/{chat_id}``               -> ``ops.get(...)``
        - ``POST /<biz>/chat/{chat_id}/confirm``       -> ``ops.confirm(...)``
        - ``POST /<biz>/chat/{chat_id}/feedback``      -> ``ops.feedback(...)``

   orchestrator 不做"前端手改 row 后回写"那种 PATCH 操作 -- 数据落库是 Java 后端的事,
   前端拿到 ``latest_rows`` 后直接交给 Java, 本 API 不做编辑.

M2M 端口 (``POST /<biz>``) 完全跳过 orchestrator, 直接调 ``handler.execute`` 拿结果,
不进 session 机制 -- 适合 Java 后端串行调用, 不需要"人在环路"的场景.
"""
from .handler import BusinessFailure, ChatHandler, ChatPlan
from .registry import HANDLERS, get as get_handler, register
from .sessions import Session, SESSIONS
from .state import SessionState
from . import ops

__all__ = [
    "BusinessFailure",
    "ChatHandler",
    "ChatPlan",
    "HANDLERS",
    "Session",
    "SESSIONS",
    "SessionState",
    "get_handler",
    "ops",
    "register",
]

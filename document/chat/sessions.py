"""Session 数据模型 + 进程内 session 表.

两个并行 dict:
    - ``SESSIONS``: ``session_id -> Session`` (可序列化 metadata + 数据)
    - ``FILES``: ``session_id -> bytes`` (raw 文件 blob, 不进 Session 避免 JSON 化)

POC 阶段不做持久化 / 多进程共享. 多 worker 部署的告警走 ``apdfi.tasks._warn_multi_worker``.
"""
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .state import SessionState


class ChatMessage(BaseModel):
    role: str  # "system" | "user" | "assistant"
    content: str


class ChatRequestMeta(BaseModel):
    """会话创建时的请求参数快照 (对齐 /excel 非 chat payload)."""

    model: str | None = None
    retry: int = 2
    sheet: int | str = 1


class Session(BaseModel):
    session_id: str
    handler_name: str
    file_name: str
    state: SessionState

    # 请求级参数快照 (跟 /excel 非 chat payload 对齐):
    # - request_meta 是规范字段 (单一真源)
    # - planner_model / planner_retry / sheet 为历史兼容字段, 后续逐步收敛
    planner_model: str | None = None
    planner_retry: int = 2
    sheet: int | str = 1
    request_meta: ChatRequestMeta = Field(default_factory=ChatRequestMeta)

    # 会话创建瞬间就由 handler.intro_message(file_name) 拼好的"前瞻"文案 (e.g. "我收到了您的
    # 文件 xxx, 接下来会按 yyy 规则解析, 预计 N 秒后给您 plan 总结"). 前端 POST 立刻就能
    # 拿到, 不必等 LLM, UI 空白期有东西展示. **不会**随 LLM 输出刷新, 是只读的初始介绍.
    initial_notice: str | None = None

    # 会话创建瞬间 handler.build_intent(file_name) 给出的**结构化**预设方案: 推断的
    # 文件元数据 (plant / period / type) + 字段映射预设 + 业务方可参考的澄清问题列表 等.
    # 跟 ``initial_notice`` 互补: notice 是 prose, intent 是结构化字段, 前端两边都能用.
    # 跟 LLM 无关, 由 handler 用文件名 regex + 业务规则拼出来.
    intent: dict[str, Any] | None = None

    # LLM 多轮对话历史, instructor / raw chat completion 都直接吃这个.
    messages: list[ChatMessage] = Field(default_factory=list)

    # 最近一次产物. 用 dict 而不是泛型 BaseModel, 避免 Session 自己也变成泛型.
    # 业务的 typed Session 子类 (e.g. ``XpengZqSession``) 把这两个字段收窄到具体的
    # ``<Customer>Plan`` / ``list[<Customer>Row]``, OpenAPI 才能给前端展示完整 schema.
    latest_plan: dict[str, Any] | None = None
    latest_rows: list[dict[str, Any]] | None = None

    # 仅在 AWAITING_FEEDBACK 状态下非空 (LLM 写完的失败诊断 prose).
    diagnosis: str | None = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


SESSIONS: dict[str, Session] = {}
FILES: dict[str, bytes] = {}


def create(
    *,
    handler_name: str,
    file_name: str,
    raw: bytes,
    initial_notice: str | None = None,
    intent: dict[str, Any] | None = None,
    planner_model: str | None = None,
    planner_retry: int = 2,
    sheet: int | str = 1,
) -> Session:
    """创建 session, 初始 state=INTENT_PREVIEW (不调 LLM).

    业务方拿到 session_id 后, 看完 ``intent`` 决定要不要继续 -> 调
    ``POST /chat/{chat_id}/start`` 才真正触发 LLM planning.
    """
    sid = uuid4().hex
    sess = Session(
        session_id=sid,
        handler_name=handler_name,
        file_name=file_name,
        state=SessionState.INTENT_PREVIEW,
        planner_model=planner_model,
        planner_retry=planner_retry,
        sheet=sheet,
        request_meta=ChatRequestMeta(model=planner_model, retry=planner_retry, sheet=sheet),
        initial_notice=initial_notice,
        intent=intent,
    )
    SESSIONS[sid] = sess
    FILES[sid] = raw
    return sess


def get(session_id: str) -> Session | None:
    return SESSIONS.get(session_id)


def file_of(session_id: str) -> bytes | None:
    return FILES.get(session_id)


def update(session_id: str, **changes) -> Session | None:
    """原地修改 session 字段. 任何字段更新都会刷新 ``updated_at``."""
    sess = SESSIONS.get(session_id)
    if sess is None:
        return None
    for k, v in changes.items():
        setattr(sess, k, v)
    sess.updated_at = datetime.utcnow()
    return sess


def append_message(session_id: str, role: str, content: str) -> None:
    sess = SESSIONS.get(session_id)
    if sess is None:
        return
    sess.messages.append(ChatMessage(role=role, content=content))
    sess.updated_at = datetime.utcnow()

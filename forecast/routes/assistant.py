"""Assistant Bot 路由 — /api/v1/assistant/*

纯对话，无 tool calling。根据 skill 计算逻辑 + 用户选中数据解读预测结果。
内存存储，会话无活动 1 小时后自动过期。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from forecast.database import get_db
from forecast.db_models import SkillORM
from forecast.models.assistant import (
    AssistantSession,
    AssistantSessionCreate,
    AssistantMessage,
    AssistantMessageRequest,
)
from forecast.llm import chat_stream, chat

router = APIRouter(prefix="/api/v1/assistant", tags=["assistant"])

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 内存存储
# ---------------------------------------------------------------------------
_SESSIONS: dict[str, AssistantSession] = {}
_SESSION_TTL = 3600  # 1 小时无活动过期


def _build_system_prompt(skill: SkillORM, data: dict[str, Any] | list[Any]) -> str:
    """根据 skill 和用户数据构建 system prompt。"""
    parts = [
        "你是预测业务助手，负责用通俗易懂的语言解读预测计算结果。",
        "",
        "## 当前使用的预测方法",
        f"- 名称：{skill.name}",
        f"- 类型：{skill.skill_type}",
    ]

    if skill.description:
        parts.append(f"- 描述：{skill.description}")

    # 计算逻辑
    parts.append("")
    parts.append("### 计算逻辑")
    if skill.skill_type == "preset" and skill.preset_name:
        parts.append(f"预设方法：{skill.preset_name}")
    elif skill.skill_type == "dsl" and skill.dsl_expression:
        parts.append(f"DSL 表达式：{skill.dsl_expression}")
    elif skill.skill_type == "python" and skill.python_code:
        parts.append("```python")
        parts.append(skill.python_code)
        parts.append("```")

    # 用户数据
    parts.append("")
    parts.append("## 用户选中的数据（含预测结果）")
    parts.append("```json")
    parts.append(json.dumps(data, ensure_ascii=False, indent=2))
    parts.append("```")

    parts.append("")
    parts.append("请基于以上计算逻辑和数据，回答用户的问题。")
    parts.append("保持简洁专业，使用中文回复。如果数据中有异常值或值得注意的趋势，请主动指出。")

    return "\n".join(parts)


def _cleanup_expired() -> None:
    """清理过期会话。"""
    now = monotonic()
    expired = [
        sid for sid, s in _SESSIONS.items()
        if now - s.updated_at > _SESSION_TTL
    ]
    for sid in expired:
        del _SESSIONS[sid]
    if expired:
        log.info("assistant: cleaned %d expired sessions", len(expired))


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

@router.post("/sessions", response_model=AssistantSession)
def create_session(payload: AssistantSessionCreate, db: Session = Depends(get_db)):
    """创建 Assistant 会话。加载 skill 信息，构建 system prompt。"""
    _cleanup_expired()

    # 加载 skill
    orm = db.query(SkillORM).filter(SkillORM.id == payload.skill_id).first()
    if not orm:
        raise HTTPException(status_code=404, detail="Skill not found")

    session = AssistantSession(
        title=payload.title,
        skill_id=payload.skill_id,
        data=payload.data,
        system_prompt=_build_system_prompt(orm, payload.data),
    )

    _SESSIONS[session.id] = session
    log.info("assistant session %s created (skill=%s)", session.id, orm.name)
    return session


@router.get("/sessions", response_model=list[AssistantSession])
def list_sessions():
    """列出所有 Assistant 会话（最新在前）。"""
    _cleanup_expired()
    sessions = sorted(_SESSIONS.values(), key=lambda s: s.updated_at, reverse=True)
    # 返回给前端时去掉 system_prompt
    return [
        s.model_copy(update={"system_prompt": ""}) for s in sessions
    ]


@router.get("/sessions/{session_id}", response_model=AssistantSession)
def get_session(session_id: str):
    """获取单个会话详情。"""
    _cleanup_expired()
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.updated_at = datetime.now(timezone.utc).timestamp()
    return session.model_copy(update={"system_prompt": ""})


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    """删除一个会话。"""
    session = _SESSIONS.pop(session_id, None)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/messages", response_model=list[AssistantMessage])
def get_messages(session_id: str):
    """获取会话的所有消息。"""
    _cleanup_expired()
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    session.updated_at = datetime.now(timezone.utc).timestamp()
    return session.messages


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    payload: AssistantMessageRequest,
    stream: bool = Query(True),
):
    """发送消息。

    - stream=true（默认）：返回 SSE 事件流
    - stream=false：返回完整 JSON
    """
    _cleanup_expired()
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # 构建消息列表
    messages = [{"role": "system", "content": session.system_prompt}]
    for m in session.messages:
        messages.append({"role": m.role, "content": m.content})

    # 追加用户新消息
    user_msg = AssistantMessage(role="user", content=payload.message)
    session.messages.append(user_msg)
    messages.append({"role": "user", "content": payload.message})

    session.updated_at = datetime.now(timezone.utc).timestamp()

    # ---- 非流式模式 ----
    if not stream:
        try:
            result = await chat(messages)
        except Exception as exc:
            log.exception("assistant chat error session=%s", session_id)
            raise HTTPException(status_code=500, detail=f"LLM error: {exc}")

        reply_content = result.get("content", "")
        reply_msg = AssistantMessage(role="assistant", content=reply_content)
        session.messages.append(reply_msg)
        session.updated_at = datetime.now(timezone.utc).timestamp()

        return {
            "session_id": session_id,
            "content": reply_content,
        }

    # ---- 流式模式 (SSE) ----
    async def event_generator():
        assistant_content = ""

        try:
            async for event in chat_stream(messages):
                if event.get("delta"):
                    assistant_content += event["delta"]
                    yield {
                        "event": "delta",
                        "data": json.dumps({"content": event["delta"]}, ensure_ascii=False),
                    }
                elif event.get("finish"):
                    # 流结束
                    break

        except Exception:
            log.exception("assistant SSE error session=%s", session_id)
            yield {
                "event": "error",
                "data": json.dumps({"error": "Internal server error"}, ensure_ascii=False),
            }
            return

        # 保存 assistant 回复
        if assistant_content:
            assistant_msg = AssistantMessage(role="assistant", content=assistant_content)
            session.messages.append(assistant_msg)
            session.updated_at = datetime.now(timezone.utc).timestamp()

        yield {
            "event": "done",
            "data": json.dumps({"session_id": session_id}, ensure_ascii=False),
        }

    return EventSourceResponse(event_generator())

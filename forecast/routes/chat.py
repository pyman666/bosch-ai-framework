"""聊天会话路由 — /api/v1/chat/*"""

from __future__ import annotations

import json
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sse_starlette.sse import EventSourceResponse

from forecast.database import get_db, SessionLocal
from forecast.db_models import ChatSessionORM
from forecast.core.memory import compact_messages_for_agent, delete_session_memory, save_session_snapshot
from forecast.utils import utcnow as _utcnow
from forecast.models.chat import (
    ChatSession, ChatSessionCreate, ChatMessage,
    ChatRequest,
)
from forecast.core.agent import agent_loop_stream, agent_non_streaming, build_display_messages

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


def _load_session_messages(session_id: str, db: Session) -> list[dict]:
    """从 DB 加载会话消息。"""
    orm = db.query(ChatSessionORM).filter(ChatSessionORM.id == session_id).first()
    if not orm:
        raise HTTPException(status_code=404, detail="Session not found")
    return orm.get_messages()


def _save_messages_bg(session_id: str, messages: list[dict]) -> None:
    """BackgroundTasks 回调 — 将消息持久化到 DB（自行开 session，不依赖请求级 db）。"""
    db = SessionLocal()
    try:
        orm = db.query(ChatSessionORM).filter(ChatSessionORM.id == session_id).first()
        if orm:
            orm.set_messages(messages)
            orm.updated_at = _utcnow()
            db.commit()
            save_session_snapshot(
                session_id=session_id,
                messages=orm.get_messages(),
                input_data=orm.get_input_data(),
                target_skill_id=orm.target_skill_id,
            )
    except SQLAlchemyError:
        log.exception("Failed to save session %s", session_id)
    except OSError:
        log.exception("IO error saving session %s", session_id)
    finally:
        db.close()


def _orm_to_pydantic(orm: ChatSessionORM) -> ChatSession:
    return ChatSession(
        id=orm.id,
        title=orm.title,
        messages=[ChatMessage(**m) for m in orm.get_messages()],
        input_data=orm.get_input_data(),
        target_skill_id=orm.target_skill_id,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

@router.post("/sessions", response_model=ChatSession)
def create_session(payload: ChatSessionCreate, db: Session = Depends(get_db)):
    """创建新的聊天会话。"""
    session = ChatSession(title=payload.title, input_data=payload.input_data)
    orm = ChatSessionORM(
        id=session.id,
        title=session.title,
    )
    orm.set_input_data(session.input_data)
    db.add(orm)
    db.commit()
    db.refresh(orm)
    return _orm_to_pydantic(orm)


@router.get("/sessions", response_model=list[ChatSession])
def list_sessions(db: Session = Depends(get_db)):
    """列出所有聊天会话（最新在前）。"""
    orms = db.query(ChatSessionORM).order_by(ChatSessionORM.updated_at.desc()).all()
    return [_orm_to_pydantic(o) for o in orms]


@router.get("/sessions/{session_id}", response_model=ChatSession)
def get_session(session_id: str, db: Session = Depends(get_db)):
    """按 ID 获取单个聊天会话。"""
    orm = db.query(ChatSessionORM).filter(ChatSessionORM.id == session_id).first()
    if not orm:
        raise HTTPException(status_code=404, detail="Session not found")
    return _orm_to_pydantic(orm)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, db: Session = Depends(get_db)):
    """删除一个聊天会话。"""
    orm = db.query(ChatSessionORM).filter(ChatSessionORM.id == session_id).first()
    if not orm:
        raise HTTPException(status_code=404, detail="Session not found")
    db.delete(orm)
    db.commit()
    delete_session_memory(session_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessage])
def get_messages(session_id: str, db: Session = Depends(get_db)):
    """获取聊天会话的所有消息。"""
    orm = db.query(ChatSessionORM).filter(ChatSessionORM.id == session_id).first()
    if not orm:
        raise HTTPException(status_code=404, detail="Session not found")
    return [ChatMessage(**m) for m in orm.get_messages()]


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    payload: ChatRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    stream: bool = True,
):
    """发送消息 — stream=true 返回 SSE 流，stream=false 返回完整 JSON。"""
    orm = db.query(ChatSessionORM).filter(ChatSessionORM.id == session_id).first()
    if not orm:
        raise HTTPException(status_code=404, detail="Session not found")

    memory_msgs = _load_session_messages(session_id, db)

    # 追加用户消息
    user_msg = {"role": "user", "content": payload.message}
    memory_msgs.append(user_msg)

    # 如果提供了附加数据则写入
    if payload.attach_data:
        orm.set_input_data(payload.attach_data)
        db.commit()

    input_data = orm.get_input_data()
    if input_data and not isinstance(input_data, list):
        input_data = [input_data]  # 单条记录包装为列表
    agent_messages = compact_messages_for_agent(
        session_id=session_id,
        messages=memory_msgs,
        input_data=input_data,
        target_skill_id=orm.target_skill_id,
    )

    # ---- 非流式模式 ----
    if not stream:
        try:
            working = await agent_non_streaming(
                session_id=session_id,
                messages=agent_messages,
                input_data=input_data,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Agent error: {exc}")

        # working = [system] + memory_msgs + 本轮新产生的 assistant/tool 消息。
        # 只提取本轮新增 assistant 内容，避免把历史 assistant 回复重复追加到会话。
        new_messages = working[len(agent_messages) + 1:]
        reply_content = ""
        for msg in new_messages:
            if msg["role"] == "assistant" and msg.get("content"):
                reply_content += (msg.get("content") or "")

        memory_msgs.append({"role": "assistant", "content": reply_content})
        background_tasks.add_task(_save_messages_bg, session_id, memory_msgs)

        return {
            "session_id": session_id,
            "content": reply_content,
            "messages": build_display_messages(working),
        }

    # ---- 流式模式 (SSE) ----
    async def event_generator():
        assistant_content = ""
        tool_calls_log = []

        try:
            async for event in agent_loop_stream(
                session_id=session_id,
                messages=agent_messages,
                input_data=input_data,
            ):
                if event["type"] == "delta":
                    assistant_content += event["content"]
                    yield {
                        "event": "delta",
                        "data": json.dumps({"content": event["content"]}, ensure_ascii=False),
                    }

                elif event["type"] == "tool_call":
                    tool_calls_log.append(event)
                    yield {
                        "event": "tool_call",
                        "data": json.dumps({
                            "name": event["name"],
                            "args": event.get("args", {}),
                        }, ensure_ascii=False),
                    }

                elif event["type"] == "tool_result":
                    yield {
                        "event": "tool_result",
                        "data": json.dumps({
                            "name": event["name"],
                            "result": event["result"],
                        }, ensure_ascii=False),
                    }

                elif event["type"] == "done":
                    break

        except Exception:
            log.exception("SSE stream error in session %s", session_id)
            yield {
                "event": "error",
                "data": json.dumps({"error": "Internal server error"}, ensure_ascii=False),
            }
            return

        # 保存到 DB (后台)
        memory_msgs.append({"role": "assistant", "content": assistant_content})
        background_tasks.add_task(_save_messages_bg, session_id, memory_msgs)

        yield {
            "event": "done",
            "data": json.dumps({"session_id": session_id}, ensure_ascii=False),
        }

    return EventSourceResponse(event_generator())


# ---------------------------------------------------------------------------
# Confirm → generate skill
# ---------------------------------------------------------------------------

@router.post("/sessions/{session_id}/confirm")
async def confirm_session(session_id: str, db: Session = Depends(get_db)):
    """确认对话并触发 Skill 生成。

    返回 calculation_logic.md、skill.md 及生成的 Skill。
    """
    from forecast.core.orchestrator import generate_skill_from_chat

    orm = db.query(ChatSessionORM).filter(ChatSessionORM.id == session_id).first()
    if not orm:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = _load_session_messages(session_id, db)

    if len(messages) < 2:
        raise HTTPException(status_code=400, detail="Not enough conversation to generate a skill")

    try:
        result = await generate_skill_from_chat(
            session_id=session_id,
            messages=messages,
            db=db,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Skill generation failed: {exc}")

    # 关联会话与 skill
    orm.target_skill_id = result["skill"].id
    orm.updated_at = _utcnow()
    db.commit()
    save_session_snapshot(
        session_id=session_id,
        messages=messages,
        input_data=orm.get_input_data(),
        target_skill_id=orm.target_skill_id,
    )

    return {
        "session_id": session_id,
        "status": "confirmed",
        "skill_id": result["skill"].id,
        "skill": result["skill"].model_dump(mode="json"),
        "calculation_logic_md": result["calculation_logic_md"],
        "skill_md": result["skill_md"],
        "repair_attempts": result.get("repair_attempts", 0),
    }

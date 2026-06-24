"""对话接口 — 自然语言查数."""

import logging
import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from analytics.models.request import ChatRequest
from analytics.models.response import ChatResponse
from analytics.core.agent import run_agent, run_agent_stream

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """自然语言查数（非流式）."""
    session_id = request.session_id or str(uuid.uuid4())
    try:
        result = await run_agent(
            user_message=request.message,
            session_id=session_id,
        )
        return ChatResponse(
            reply=result["reply"],
            chart=result.get("chart"),
            data=result.get("data"),
            sources=result.get("sources", []),
            session_id=session_id,
            insights=result.get("insights"),
        )
    except Exception as e:
        log.error(f"[chat] 处理失败: {e}", exc_info=True)
        return ChatResponse(
            reply="抱歉，处理你的问题时出了点错误，请稍后重试。",
            chart=None,
            data=None,
            sources=[],
            session_id=session_id,
        )


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """自然语言查数（流式 SSE）."""
    session_id = request.session_id or str(uuid.uuid4())

    async def event_generator():
        try:
            async for event in run_agent_stream(
                user_message=request.message,
                session_id=session_id,
            ):
                yield event
        except Exception as e:
            log.error(f"[chat/stream] 流式处理失败: {e}", exc_info=True)
            yield f"event: error\ndata: {{\"type\": \"error\", \"message\": \"{e}\"}}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx 不缓冲 SSE
            "X-Session-Id": session_id,
        },
    )

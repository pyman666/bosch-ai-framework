"""Chat session 的业务无关操作层.

每个函数对应一种状态转移. 业务路由层只负责 (1) 类型化输入/输出 (2) 调用这里的 op.

LLM 调用 (plan / re-plan / diagnose) 走 ``apdfi.tasks`` 异步, 但**对客户隐藏 task_id**:
客户只看 ``GET /chat/{chat_id}`` 里的 ``state`` 字段判断是否还在算.
"""
import logging

from fastapi import BackgroundTasks, HTTPException

from infra.llm import aclient
from infra.settings import DEFAULT_MODEL
from .diagnose import safe_run
from .handler import ChatHandler
from .registry import get as get_handler
from .sessions import (
    ChatRequestMeta,
    Session,
    append_message,
    create as create_session,
    file_of,
    get as get_session,
    update as update_session,
)
from .state import ALLOWED_ENTRIES, SessionState


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# helpers

def _require(session_id: str) -> Session:
    sess = get_session(session_id)
    if sess is None:
        raise HTTPException(404, f"会话 {session_id!r} 不存在 (session_id 错误或已过期)")
    return sess


def _check_state(sess: Session, op: str) -> None:
    allowed = ALLOWED_ENTRIES.get(op, set())
    if sess.state not in allowed:
        raise HTTPException(
            409,
            f"当前会话状态为 {sess.state.value!r}, 不允许执行 {op!r} 操作; "
            f"该操作仅在以下状态下可用: {[s.value for s in allowed]}",
        )


def _handler_of(sess: Session) -> ChatHandler:
    return get_handler(sess.handler_name)


# ---------------------------------------------------------------------------
# LLM ops

async def _call_planner(
    handler: ChatHandler,
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    retry: int = 2,
):
    """跟 instructor 要一个 ``handler.PlanSchema`` 实例, 不做异常包装."""
    return await aclient.chat.completions.create(
        model=model,
        messages=messages,
        response_model=handler.PlanSchema,
        max_retries=max(retry, 0),
    )


def _serialize_messages(sess: Session) -> list[dict]:
    return [{"role": m.role, "content": m.content} for m in sess.messages]


async def _bg_plan(session_id: str, *, model: str, retry: int) -> None:
    """后台任务: 出 plan, 写回 session, 状态转 ``AWAITING_CONFIRM``."""
    sess = get_session(session_id)
    if sess is None:
        return
    handler = _handler_of(sess)

    try:
        plan = await _call_planner(
            handler,
            _serialize_messages(sess),
            model=model,
            retry=retry,
        )
    except Exception as e:
        logger.exception("plan failed for session %s", session_id)
        update_session(
            session_id,
            state=SessionState.AWAITING_FEEDBACK,
            diagnosis=f"LLM 出 plan 失败: {type(e).__name__}: {e}. 请提供更具体的提示后重试.",
        )
        return

    plan_dict = plan.model_dump()
    update_session(
        session_id,
        state=SessionState.AWAITING_CONFIRM,
        latest_plan=plan_dict,
        diagnosis=None,
    )
    # 把 assistant 的 plan 输出存进 messages, 方便后续 feedback 接着对话.
    append_message(session_id, "assistant", plan.model_dump_json())


# ---------------------------------------------------------------------------
# public ops

async def start(
    *,
    handler_name: str,
    file_name: str,
    raw: bytes,
    model: str = DEFAULT_MODEL,
    retry: int = 2,
    sheet: int | str = 1,
) -> Session:
    """开 session, **不**触发 LLM. 立即返回 (state=INTENT_PREVIEW).

    返回的 session 已经填好两个给前端的字段, 前端 POST 完就能直接渲染:

    - ``initial_notice``: handler 写的中文 prose, "我接下来会做什么".
    - ``intent``: handler 写的**结构化**预设方案 (推断元数据 / 字段映射 / 澄清问题列表 等),
      handler 不实现 ``build_intent`` 时为 ``None``, 此时前端跳过 'AI Tip' 步骤直接走
      ``POST /chat/{chat_id}/start`` 即可.

    业务方看完 intent 决定继续 -> 调 ``start_planning(session_id, ...)`` 才真正触发 LLM.
    """
    handler = get_handler(handler_name)
    intent_obj = handler.build_intent(file_name)
    intent = intent_obj.model_dump() if intent_obj is not None else None

    return create_session(
        handler_name=handler_name,
        file_name=file_name,
        raw=raw,
        planner_model=model,
        planner_retry=max(retry, 0),
        sheet=sheet,
        initial_notice=handler.intro_message(file_name),
        intent=intent,
    )


def _kick_planning(
    handler: ChatHandler,
    session_id: str,
    *,
    raw: bytes,
    sheet: int | str,
    model: str,
    clarifications_text: str | None,
    bg: BackgroundTasks,
    retry: int,
) -> None:
    """把 system + user message 塞进 session, 状态转 PLANNING, 后台调度首轮 plan."""
    skeleton = handler.build_skeleton(raw, sheet=sheet)
    user_first = skeleton if not clarifications_text else f"{skeleton}\n\n## 业务方临时补充\n{clarifications_text}"
    append_message(session_id, "system", handler.plan_prompt)
    append_message(session_id, "user", user_first)
    update_session(session_id, state=SessionState.PLANNING, diagnosis=None)
    bg.add_task(_bg_plan, session_id, model=model, retry=retry)


async def start_planning(
    session_id: str,
    *,
    clarifications: str | None,
    bg: BackgroundTasks,
    model: str | None = None,
    retry: int | None = None,
) -> Session:
    """业务方看完 intent 决定继续, 把澄清答复 (可选) 一起拼进 LLM prompt, 转 PLANNING.

    - 仅允许从 ``INTENT_PREVIEW`` 进入.
    - ``clarifications``: 业务方在 "complex chat" 步骤里答完的若干 Q&A, 已经被前端拼成
      一段中文 prose. 例: ``"日期表头在第 5 行; 月度桶; 忽略隐藏列"``. None 表示业务方
      选择"按预设直接解析", 不附加任何额外提示.
    """
    sess = _require(session_id)
    _check_state(sess, "start_planning")
    handler = _handler_of(sess)
    raw = file_of(session_id)
    if raw is None:
        raise HTTPException(410, f"会话 {session_id!r} 的文件已不在内存, 请重新创建会话")
    planner_model = model or sess.request_meta.model or sess.planner_model or DEFAULT_MODEL
    planner_retry = max((sess.request_meta.retry if retry is None else retry), 0)
    planner_sheet = sess.request_meta.sheet if sess.request_meta else sess.sheet
    update_session(
        session_id,
        planner_model=planner_model,
        planner_retry=planner_retry,
        sheet=planner_sheet,
        request_meta=ChatRequestMeta(model=planner_model, retry=planner_retry, sheet=planner_sheet),
    )
    _kick_planning(
        handler,
        session_id,
        raw=raw,
        sheet=planner_sheet,
        model=planner_model,
        clarifications_text=clarifications,
        bg=bg,
        retry=planner_retry,
    )
    return _require(session_id)


async def get(session_id: str) -> Session:
    return _require(session_id)


async def confirm(session_id: str) -> Session:
    """同步: 拿当前 plan 去 execute. 成功 -> DONE; 业务失败 -> 后台跑诊断 -> AWAITING_FEEDBACK."""
    sess = _require(session_id)
    _check_state(sess, "confirm")

    handler = _handler_of(sess)
    plan = handler.PlanSchema.model_validate(sess.latest_plan)
    raw = file_of(session_id)

    rows, diagnosis = await safe_run(
        handler.execute(
            plan,
            raw,
            sheet=(sess.request_meta.sheet if sess.request_meta else sess.sheet),
        ),
        handler=handler,
        ctx={"plan_summary": plan.summary},
    )

    if diagnosis is not None:
        # 失败 -> 把诊断也以 assistant 身份并入对话, 后续 feedback 会带这段上下文.
        append_message(session_id, "assistant", f"[诊断] {diagnosis}")
        return update_session(
            session_id,
            state=SessionState.AWAITING_FEEDBACK,
            diagnosis=diagnosis,
            latest_rows=None,
        )

    return update_session(
        session_id,
        state=SessionState.DONE,
        latest_rows=[r.model_dump() for r in rows],
        diagnosis=None,
    )


async def feedback(
    session_id: str,
    *,
    text: str,
    bg: BackgroundTasks,
    retry: int = 2,
) -> Session:
    """异步: 把 user 反馈 append 到 messages, 后台重新跑 plan. 客户轮 GET 看 state."""
    sess = _require(session_id)
    _check_state(sess, "feedback")

    append_message(session_id, "user", text)
    update_session(session_id, state=SessionState.PLANNING, diagnosis=None)
    planner_model = sess.request_meta.model or sess.planner_model or DEFAULT_MODEL
    bg.add_task(_bg_plan, session_id, model=planner_model, retry=retry)
    return _require(session_id)

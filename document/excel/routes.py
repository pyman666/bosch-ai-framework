"""Excel 路由总入口.

仅暴露参数化端点 (`POST /excel`, `GET /excel/data/{task_id}`, `POST /excel/chat*`).
加新客户只需在 ``clients/<name>/__init__.py`` 里 ``support(config)`` 声明, 本文件
通过 pkgutil 自动发现子包并装载注册表.
"""
import importlib
import pkgutil
from dataclasses import replace as _dc_replace

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query

from .wide import _build_ack as _build_wide_ack, wide_excel_task
from .simple import SimpleExcelTaskAck, simple_excel_task
from .complex import complex_excel_task
from ._common import excel_upload
from infra.settings import DEFAULT_MODEL
from infra.task import TaskResult, create_task, get_task
from ..chat import ops as chat_ops
from . import clients as _clients_pkg
from .clients import all_labels, get_config


router = APIRouter()

# task_id -> phase, 让 ``GET /excel/data/{task_id}`` 不需要再传 client.
_TASK_PHASE: dict[str, str] = {}


# ---------------------------------------------------------------------------
# 客户端点注册: pkgutil 自动发现 clients/ 下所有子包, 由统一参数化路由派发
for _importer, _pkg_name, _ispkg in pkgutil.iter_modules(_clients_pkg.__path__):
    if not _pkg_name.startswith("_") and _ispkg:
        importlib.import_module(f".clients.{_pkg_name}", package=__package__)


def _resolve_client(client: str):
    found = get_config(client)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail={
                "code": "excel_client_not_found",
                "message": f"未知 client: {client!r}",
                "client": client,
                "available_clients": sorted(all_labels()),
            },
        )
    return found


def _resolve_chat_client(client: str):
    found = _resolve_client(client)
    engine, cfg, kwargs = found
    if engine != "complex":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "excel_chat_client_invalid",
                "message": f"client={client!r} 不是 complex 客户, 不支持 chat",
                "client": client,
                "required_engine": "complex",
                "chat_clients": sorted(all_labels(engine="complex")),
            },
        )
    return engine, cfg, kwargs


def _chat_ack_of(sess) -> dict:
    return {
        "initial_notice": sess.initial_notice,
        "intent": sess.intent,
        "model": sess.request_meta.model,
        "retry": sess.request_meta.retry,
        "sheet": sess.request_meta.sheet,
    }


def _chat_resp(sess) -> dict:
    """统一 chat 返回外壳: 顶层给前端常用字段, 不回 messages 大对象."""
    state = sess.state.value if hasattr(sess.state, "value") else str(sess.state)
    latest_plan = sess.latest_plan
    latest_rows = sess.latest_rows
    return {
        "client": sess.handler_name,
        "engine": "complex",
        "task_id": sess.session_id,
        "file_name": sess.file_name,
        "state": state,
        "ack": _chat_ack_of(sess),
        # 前端常用数据平铺, 不必再深入 session.latest_*
        "plan": latest_plan,
        "rows": latest_rows,
        "diagnosis": sess.diagnosis,
        "has_plan": latest_plan is not None,
        "has_rows": bool(latest_rows),
    }


@router.post("", summary="[M2M] 提交 Excel 解析任务 (按 client 参数自动派发)")
async def excel_create(
    tasks: BackgroundTasks,
    client: str = Form(..., description="客户标识, e.g. chery / geely-ms / xpeng-zq"),
    file_params: dict = Depends(excel_upload),
    model: str = Form(DEFAULT_MODEL, description=f"LLM 模型别名, 默认 `{DEFAULT_MODEL}`"),
    retry: int = Form(2, description="LLM 输出 schema 校验最大重试次数, ≥ 0, 默认 `2`"),
) -> dict:
    engine, cfg, kwargs = _resolve_client(client)
    phase = kwargs.get("phase", "parse")

    if engine == "simple":
        ack = await create_task(
            tasks,
            simple_excel_task,
            file_params["raw"],
            sheet=file_params["sheet"],
            config=cfg,
            model=model,
            retry=retry,
            phase=phase,
        )
        _TASK_PHASE[ack.task_id] = phase
        ack_payload = SimpleExcelTaskAck(
            task_id=ack.task_id,
            file_name=file_params["file_name"],
            column_map=cfg.column_map,
            date_column=cfg.date_column,
            target_date_format=cfg.target_date_format,
            header_row=cfg.header_row,
        ).model_dump()
        task_id = ack_payload.pop("task_id")
        file_name = ack_payload.pop("file_name")
        return {
            "client": client,
            "engine": engine,
            "task_id": task_id,
            "file_name": file_name,
            "ack": ack_payload,
        }

    if engine == "wide":
        eff_cfg = _dc_replace(cfg, sheet=file_params["sheet"])
        ack = await create_task(
            tasks,
            wide_excel_task,
            file_params["raw"],
            config=eff_cfg,
            model=model,
            retry=retry,
            phase=phase,
        )
        _TASK_PHASE[ack.task_id] = phase
        ack_payload = _build_wide_ack(
            eff_cfg,
            task_id=ack.task_id,
            file_name=file_params["file_name"],
        ).model_dump()
        task_id = ack_payload.pop("task_id")
        file_name = ack_payload.pop("file_name")
        return {
            "client": client,
            "engine": engine,
            "task_id": task_id,
            "file_name": file_name,
            "ack": ack_payload,
        }

    # complex
    ack = await create_task(
        tasks,
        complex_excel_task,
        file_params["raw"],
        config=cfg,
        model=model,
        prompt=None,
        retry=retry,
        sheet=file_params["sheet"],
        phase=phase,
    )
    _TASK_PHASE[ack.task_id] = phase
    return {
        "client": client,
        "engine": engine,
        "task_id": ack.task_id,
        "file_name": file_params["file_name"],
        "ack": {},
    }


@router.get("/data/{task_id}", summary="[M2M] 取 Excel 解析结果")
async def excel_data(
    task_id: str,
    compact: bool = Query(False, description="是否只返回轻量状态视图 (默认 `false`, 保持历史兼容)"),
) -> TaskResult:
    phase = _TASK_PHASE.get(task_id, "parse")
    res = await get_task(task_id, phase)
    if not compact:
        return res

    status = res.status.value if hasattr(res.status, "value") else (res.status or "processing")
    if status is None:
        status = "processing"
    return {
        "task_id": task_id,
        "phase": phase,
        "status": status,
        "has_result": res.result is not None,
        "has_error": bool(res.message),
    }


@router.post("/chat", summary="[Chat] 创建 Excel 解析会话 (按 client 参数自动派发)")
async def excel_chat_create(
    client: str = Form(..., description="complex 客户标识, e.g. xpeng-zq"),
    file_params: dict = Depends(excel_upload),
    model: str = Form(DEFAULT_MODEL, description=f"LLM 模型别名, 默认 `{DEFAULT_MODEL}`"),
    retry: int = Form(2, description="LLM 输出 schema 校验最大重试次数, ≥ 0, 默认 `2`"),
):
    _engine, _cfg, _kwargs = _resolve_chat_client(client)
    session = await chat_ops.start(
        handler_name=client,
        file_name=file_params["file_name"],
        raw=file_params["raw"],
        model=model,
        retry=retry,
        sheet=file_params["sheet"],
    )
    return _chat_resp(session)


@router.post("/chat/start/{task_id}", summary="[Chat] 业务方确认预设/答完澄清, 触发 LLM planning")
async def excel_chat_start(
    task_id: str,
    bg: BackgroundTasks,
    clarifications: str = Form(None, description="可选: 业务方补充的澄清答复"),
    model: str | None = Form(None, description="可选: 覆盖会话创建时的 LLM 模型"),
    retry: int | None = Form(None, description="可选: 覆盖会话创建时的 LLM 输出 schema 校验重试次数"),
):
    sess = await chat_ops.start_planning(
        task_id,
        clarifications=clarifications,
        bg=bg,
        model=model,
        retry=retry,
    )
    return _chat_resp(sess)


@router.get("/chat/{task_id}", summary="[Chat] 拉取 Excel 会话状态")
async def excel_chat_get(
    task_id: str,
    compact: bool = Query(True, description="是否只返回轻量状态视图 (默认 `true`)"),
    view: str | None = Query(
        None,
        description="可选视图: `compact` / `full` / `intent` / `plan` / `rows` / `messages`",
    ),
):
    sess = await chat_ops.get(task_id)

    # 新入口: 一个 GET + view 覆盖旧的四个子资源路由
    if view is not None:
        v = view.lower().strip()
        if v == "full":
            return sess
        if v == "compact":
            compact = True
        elif v == "intent":
            return {"task_id": sess.session_id, "intent": sess.intent}
        elif v == "plan":
            return {
                "task_id": sess.session_id,
                "state": sess.state,
                "latest_plan": sess.latest_plan,
                "diagnosis": sess.diagnosis,
            }
        elif v == "rows":
            return {
                "task_id": sess.session_id,
                "state": sess.state,
                "latest_rows": sess.latest_rows,
            }
        elif v == "messages":
            return {
                "task_id": sess.session_id,
                "messages": sess.messages,
            }
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "excel_chat_view_invalid",
                    "message": f"不支持的 view: {view!r}",
                    "view": view,
                    "allowed_views": ["compact", "full", "intent", "plan", "rows", "messages"],
                },
            )

    if not compact:
        return _chat_resp(sess)
    state = sess.state.value if hasattr(sess.state, "value") else str(sess.state)
    return {
        "task_id": sess.session_id,
        "client": sess.handler_name,
        "file_name": sess.file_name,
        "state": state,
        "has_intent": sess.intent is not None,
        "has_plan": sess.latest_plan is not None,
        "has_rows": bool(sess.latest_rows),
        "has_diagnosis": sess.diagnosis is not None,
        "planner": {
            "model": sess.request_meta.model,
            "retry": sess.request_meta.retry,
            "sheet": sess.request_meta.sheet,
        },
        "updated_at": sess.updated_at,
    }



@router.post("/chat/confirm/{task_id}", summary="[Chat] 业务方确认 plan, 同步触发抽取")
async def excel_chat_confirm(task_id: str):
    sess = await chat_ops.confirm(task_id)
    return _chat_resp(sess)


@router.post("/chat/feedback/{task_id}", summary="[Chat] 业务方提反馈, 异步触发重 plan")
async def excel_chat_feedback(
    task_id: str,
    bg: BackgroundTasks,
    text: str = Form(..., description="自然语言反馈"),
    retry: int = Form(2, description="LLM 输出 schema 校验最大重试次数, ≥ 0, 默认 `2`"),
):
    sess = await chat_ops.feedback(task_id, text=text, bg=bg, retry=retry)
    return _chat_resp(sess)

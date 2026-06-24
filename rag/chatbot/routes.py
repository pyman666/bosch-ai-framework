"""ChatBot 相关 HTTP 路由.

只暴露同一条 chatbot pipeline 的两个入口, 定位都是 BPAE / O2C 的报错 /
data 结果分析助手:

  - POST /ask   —  单轮入口: 业务客户在前端点某条数据, 前端把"路由 URL +
                   data id + 当前状态文本"打包进 query 调本接口, LLM 给出
                   客户向的诊断 + 下一步. 不带对话上下文.
  - POST /chat  —  多轮入口: 同样锚到一条数据, 但前端额外传整段对话历史
                   (user / assistant 交替, 末条 user). 服务端 stateless,
                   每轮只对末条 user 重做 RAG 检索, 之前轮次原样回放. 适合
                   客户对同一条数据反复追问 / 让助手解释上一句的场景.

URL 和 handler 都用"角色名" (``/ask`` / ``/chat`` / ``ask_bot`` / ``chat_bot``)
而非"实现名" (``/hybrid``) — 实现层换了也不影响外部调用方. 当前实现是
hybrid 检索 (deterministic lookup + BM25 + dense FAISS, 可选 cross-encoder
rerank): 通用部分在 [`bapee/rag/`](../rag/), BPAE 业务定制
(URL/payload 字段映射 + prompt 措辞) 在 [`bpae_pipeline.py`](./bpae_pipeline.py).

两个端点输出协议**完全一致**: 默认走 SSE 流 (``?stream=true``, default),
``?stream=false`` 时收完整段后再返 ``text/plain``. 前端从 ``/ask`` 切到
``/chat`` 只需要换 URL + 把 ``user_question`` 字段换成 ``messages: [...]``.

可观测性: 两个端点都记 ``<endpoint>.start`` / ``<endpoint>.done`` /
``<endpoint>.error`` 三阶段 structured 日志. 关键字段:

- ``route_url`` / ``model`` / ``stream`` — 请求关键参数
- ``payload_keys`` / ``user_question_chars`` / ``turn_count`` (chat only) — 输入规模
- ``latency_ms`` / ``first_chunk_ms`` / ``chunk_count`` / ``total_chars`` — 性能 + 输出体量
- ``request_id`` — 由 ``RequestIDMiddleware`` 自动注入, 跟客户端报障对接

线上排障 SOP: 拿到客户的 ``X-Request-ID`` → 在日志聚合 (Splunk / Loki) 里
``request_id="..." endpoint=ask`` 一搜, ``ask.start`` 看输入, ``ask.done``/
``ask.error`` 看结果, ``latency_ms`` 看慢在哪段.
"""
import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..core.auth import _bot_auth
from ..settings import STREAM_MAX_DURATION_SEC
from .bpae_pipeline import ask_bot, ask_bot_text, chat_bot, chat_bot_text
from .schemas import AskQuery, ChatQuery


logger = logging.getLogger(__name__)


router = APIRouter(dependencies=[Depends(_bot_auth)])


# SSE 末尾哨兵, 兼容 OpenAI / 大多数前端 SSE 库的"流结束"约定.
_SSE_DONE = "data: [DONE]\n\n"


def _sse_chunk(text: str) -> str:
    """把一片 LLM 增量包成 SSE event.

    用 ``json.dumps(ensure_ascii=False)`` 转义内容: 即可保留中文可读, 又
    顺手把 ``\\n`` / 引号等 escape 了 — SSE 协议本身要求 ``data:`` 字段
    内不能有裸换行 (会被解析成多 event), JSON 转义正好规避这个坑.
    """
    return f"data: {json.dumps({'delta': text}, ensure_ascii=False)}\n\n"


def _sse_error(err: BaseException) -> str:
    """流中段失败时, 用 SSE error event 通知前端 (而不是悄悄断连接)."""
    payload = {"error": err.__class__.__name__, "message": str(err)}
    return f"event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_timeout(elapsed_sec: float, limit_sec: float) -> str:
    """流超过 wall-clock 上限被主动截断, 给前端一个明确的 ``event: timeout``.

    跟 ``event: error`` 故意区分: timeout 不是失败 (回答可能是真的太长),
    前端 UI 应该提示用户"回答被截断, 可重新提问要求更简短" 而不是当作 5xx.
    """
    payload = {
        "elapsed_sec": round(elapsed_sec, 2),
        "limit_sec": round(limit_sec, 2),
        "message": "stream truncated by server wall-clock limit",
    }
    return f"event: timeout\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_shutdown() -> str:
    """server 滚动发布 / 收到 SIGTERM 主动截断流时, 给前端一个明确的 ``event: shutdown``.

    跟 ``event: timeout`` / ``event: error`` 区分: shutdown 不是错也不是
    回答太长, 是 server 自己要退出. 前端 UI 应该提示"服务正在更新, 请稍后
    重试" + 自动重发 (新流量已经被切到新实例了).
    """
    payload = {"message": "server is shutting down; please retry"}
    return f"event: shutdown\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _ms_since(started: float) -> int:
    """``time.monotonic()`` 时间戳差转毫秒 (int, 避免 float 噪音)."""
    return int((time.monotonic() - started) * 1000)


async def _with_timeout(
    coro,
    *,
    ctx_name: str,
    log_ctx: dict[str, Any],
    started: float,
):
    """非流式路径包一层 ``asyncio.wait_for`` 给 wall-clock 上限.

    流式路径在每 chunk 之间已主动检查 elapsed, 不走这里. 非流式只有一次大
    await, 没主动检查机会, 用 asyncio 内置的 wait_for 是最干净的取消方式.

    超时表现: 抛 ``HTTPException(504, "stream truncated by server ...")`` —
    跟 SSE 那边 ``event: timeout`` 语义对齐, 客户端看到 504 应该跟看到
    timeout event 一样处理 (回答太长 / 让 LLM 简短点).
    """
    cap = STREAM_MAX_DURATION_SEC
    if cap <= 0:
        return await coro
    try:
        return await asyncio.wait_for(coro, timeout=cap)
    except asyncio.TimeoutError:
        logger.warning(
            f"{ctx_name}.timeout",
            extra={
                **log_ctx,
                "outcome": "timeout",
                "latency_ms": _ms_since(started),
                "limit_sec": cap,
            },
        )
        raise HTTPException(
            status_code=504,
            detail=(
                f"server wall-clock limit ({cap:.0f}s) exceeded; "
                f"ask for a shorter answer or retry later"
            ),
        )


def _ask_log_ctx(req: AskQuery, stream: bool) -> dict[str, Any]:
    """构造 ``/ask`` 的结构化日志公共字段 (start/done/error 三阶段共用).

    只记**入参的摘要元数据** (字段数 / 长度), 不记 payload 内容本身 — 客户
    传的可能含 PII (订单号 / 业务关键字), 别让它进日志聚合系统. 想看具体
    内容时业务层 / 调试模式临时加.
    """
    return {
        "endpoint": "ask",
        "route_url": req.route_url,
        "model": req.model or "default",
        "stream": stream,
        "payload_keys": len(req.payload or {}),
        "user_question_chars": len(req.user_question or ""),
    }


def _chat_log_ctx(req: ChatQuery, stream: bool) -> dict[str, Any]:
    """``/chat`` 版的 :func:`_ask_log_ctx`, 多两个对话维度指标."""
    user_turns = sum(1 for m in req.messages if m.role == "user")
    history_chars = sum(len(m.content) for m in req.messages)
    return {
        "endpoint": "chat",
        "route_url": req.route_url,
        "model": req.model or "default",
        "stream": stream,
        "payload_keys": len(req.payload or {}),
        "turn_count": user_turns,
        "history_len": len(req.messages),
        "history_chars": history_chars,
    }


async def _logged_stream(
    name: str,
    base_ctx: dict[str, Any],
    started: float,
    upstream: AsyncIterator[str],
    request: Request,
) -> AsyncIterator[str]:
    """统一的 SSE 流包装: 把 LLM 增量包成 SSE event + 三件事:

    1. **客户端断开主动取消** — 每片 chunk yield 前 ``await request.is_disconnected()``,
       侦测到客户掉线就 ``break`` 并 ``await upstream.aclose()``, 把取消信号
       传到底层 LiteLLM HTTP 请求, 不继续烧 LLM token. 客户关浏览器 / 切页面
       这种常见取消, 后端能秒级释放配额, 别忽略.
    2. **TTFB / 全流时延 / 体量度量** — ``chunk_count`` (流了多少片),
       ``total_chars`` (总输出量), ``first_chunk_ms`` (TTFB), ``latency_ms``
       (整流总时长). 上线后看 TTFB 异常基本能定位 LLM provider 抖动.
    3. **三态收尾日志** — ``done`` (正常完结) / ``disconnected`` (客户中
       断) / ``error`` (流中段异常). 区分这三种很重要: ``error`` 是要追的
       bug, ``disconnected`` 是正常人类行为不该报警, ``done`` 是 baseline.

    流中段异常: 响应头已写, 改不了 status code, 在流尾追一个 ``event: error``
    让前端能感知失败而非以为是正常 EOF; 同时 server 端 ``logger.exception``
    记完整栈.
    """
    chunk_count = 0
    total_chars = 0
    first_chunk_ms: int | None = None
    disconnected = False
    timed_out = False
    shutting_down = False
    # 0 表示禁用上限 (测试 / 长生成调试用), 任何正数都是 wall-clock 秒数.
    cap_sec = STREAM_MAX_DURATION_SEC if STREAM_MAX_DURATION_SEC > 0 else float("inf")
    # 模块级 event, 由 :mod:`bapee.server` 的 lifespan 在 cleanup 期 set —
    # 进程收到 SIGTERM 进入 graceful shutdown 时, 在飞的 SSE 流主动收尾,
    # 而不是等 ``gunicorn graceful_timeout`` 到期被砍.
    from ..server import shutdown_event  # 局部 import 防 import 环
    try:
        async for chunk in upstream:
            # 主动探测断连. ``is_disconnected()`` 在 ASGI 层非阻塞 poll
            # receive channel, 拿不到就立即返 False; 这里每片调一次完全
            # 接得住. 不放在 yield 之后是因为 TCP buffer 可能让"写出去
            # 了"和"客户真的收到"之间还有秒级差, 用 receive 信号判更准.
            if await request.is_disconnected():
                disconnected = True
                break
            if shutdown_event.is_set():
                # 进程正在 graceful shutdown; 收尾给前端一个明确信号让它
                # 自动重试 (新流量已被切到新实例).
                shutting_down = True
                yield _sse_shutdown()
                break
            elapsed = time.monotonic() - started
            if elapsed >= cap_sec:
                # 主动截流. 不再消费 upstream, finally 里的 aclose 会把
                # 取消信号传到底层 LLM HTTP 请求, 节省 token.
                timed_out = True
                yield _sse_timeout(elapsed, cap_sec)
                break
            if first_chunk_ms is None:
                first_chunk_ms = _ms_since(started)
            chunk_count += 1
            total_chars += len(chunk)
            yield _sse_chunk(chunk)
        if not disconnected and not timed_out and not shutting_down:
            yield _SSE_DONE
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            f"{name}.error",
            extra={
                **base_ctx,
                "outcome": "error",
                "latency_ms": _ms_since(started),
                "first_chunk_ms": first_chunk_ms,
                "chunk_count": chunk_count,
                "total_chars": total_chars,
                "exc_type": type(exc).__name__,
            },
        )
        yield _sse_error(exc)
        return
    finally:
        # ``aclose()`` 在 upstream 已经自然消耗完时是 no-op; 在 break
        # (断连) / 抛异常时会向 upstream 注入 ``GeneratorExit``, LiteLLM
        # 那边收到后会取消底层 httpx 请求 — 真实节省 LLM 配额靠这一步.
        # 即便 aclose 自己抛, 也不应影响外层日志, 故 try/except 兜底.
        try:
            await upstream.aclose()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.debug(f"{name}: upstream aclose raised", exc_info=True)

    if disconnected:
        # info 级而不是 warning: 客户主动取消不是异常, 但需要在常规日志里
        # 能数出比例 (e.g. disconnected_rate 突然变高可能意味着前端 timeout
        # 设得太紧, 或者首 chunk 太慢客户没耐心等了).
        logger.info(
            f"{name}.disconnected",
            extra={
                **base_ctx,
                "outcome": "disconnected",
                "latency_ms": _ms_since(started),
                "first_chunk_ms": first_chunk_ms,
                "chunk_count": chunk_count,
                "total_chars": total_chars,
            },
        )
        return

    if timed_out:
        # warning 级: 触发上限不是异常但确实意味着有问题 (回答异常长 /
        # LLM 提供商卡了 / 上限调得太紧). 聚合告警监控 ``timeout_rate``
        # 突变可定位问题来源.
        logger.warning(
            f"{name}.timeout",
            extra={
                **base_ctx,
                "outcome": "timeout",
                "latency_ms": _ms_since(started),
                "first_chunk_ms": first_chunk_ms,
                "chunk_count": chunk_count,
                "total_chars": total_chars,
                "limit_sec": cap_sec,
            },
        )
        return

    if shutting_down:
        # info 级: 滚动发布期被截是预期事件, 不是异常. 但要数出比例, 防
        # "每次发布就丢一波流" 这种没人感知的退化.
        logger.info(
            f"{name}.shutdown",
            extra={
                **base_ctx,
                "outcome": "shutdown",
                "latency_ms": _ms_since(started),
                "first_chunk_ms": first_chunk_ms,
                "chunk_count": chunk_count,
                "total_chars": total_chars,
            },
        )
        return

    logger.info(
        f"{name}.done",
        extra={
            **base_ctx,
            "outcome": "ok",
            "latency_ms": _ms_since(started),
            "first_chunk_ms": first_chunk_ms,
            "chunk_count": chunk_count,
            "total_chars": total_chars,
        },
    )


@router.post(
    "/ask",
    summary="BPAE error / data 诊断 chatbot (SSE 流式, 可 ?stream=false 退回整段)",
)
async def ask_endpoint(
    request: AskQuery,
    http_request: Request,
    stream: bool = True,
):
    """报错 / data 诊断: 当前实现是 hybrid 检索 → LLM 作答.

    - ``stream=true`` (default): SSE (``text/event-stream``). 每个 event 形如
      ``data: {"delta": "..."}\\n\\n``, 流末尾发 ``data: [DONE]\\n\\n``.
      LLM 调用中段失败时发 ``event: error\\ndata: {"error":...,"message":...}\\n\\n``.
    - ``stream=false``: 收完整段后一次性返 ``text/plain``. 适合脚本 / curl /
      不需要流式 UX 的调用方.

    前端提交结构化字段 (``route_url`` + ``payload`` + 可选 ``user_question``);
    本路由把它们透传给 ``ask_bot``, 由 pipeline 层负责检索拼装与 LLM 调用.
    """
    started = time.monotonic()
    log_ctx = _ask_log_ctx(request, stream)
    logger.info("ask.start", extra=log_ctx)

    if not stream:
        try:
            text = await _with_timeout(
                ask_bot_text(
                    route_url=request.route_url,
                    payload=request.payload,
                    user_question=request.user_question,
                    model=request.model,
                ),
                ctx_name="ask",
                log_ctx=log_ctx,
                started=started,
            )
        except Exception as exc:
            if not isinstance(exc, HTTPException):
                logger.exception(
                    "ask.error",
                    extra={
                        **log_ctx,
                        "outcome": "error",
                        "latency_ms": _ms_since(started),
                        "exc_type": type(exc).__name__,
                    },
                )
            raise
        logger.info(
            "ask.done",
            extra={
                **log_ctx,
                "outcome": "ok",
                "latency_ms": _ms_since(started),
                "total_chars": len(text),
            },
        )
        return PlainTextResponse(text)

    upstream = ask_bot(
        route_url=request.route_url,
        payload=request.payload,
        user_question=request.user_question,
        model=request.model,
    )
    return StreamingResponse(
        _logged_stream("ask", log_ctx, started, upstream, http_request),
        media_type="text/event-stream",
        headers={
            # 关 nginx 缓冲, 防止 SSE chunk 在 reverse proxy 那段被攒包.
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


@router.post(
    "/chat",
    summary="BPAE error / data 诊断 chatbot (多轮 chat 版; SSE 流式, 可 ?stream=false 退回整段)",
)
async def chat_endpoint(
    request: ChatQuery,
    http_request: Request,
    stream: bool = True,
):
    """多轮 chat 版的 ``/ask``: 锚定到同一条数据, 但带前端持有的对话历史.

    协议 / 响应格式跟 ``/ask`` 完全一致 (同样的 SSE event 形状 + ``[DONE]``
    哨兵 + ``event: error``), 前端从 ``/ask`` 切到 ``/chat`` 只需要换 URL +
    把 ``user_question`` 换成 ``messages: [...]``.

    - ``messages``: 完整对话历史, ``user`` / ``assistant`` 严格交替, 末条
      必须是 ``user`` (schema 层校验, 不合法直接 422); 首轮就是单元素
      ``[{role:"user", content:"..."}]``.
    - 服务端 stateless: 不存 session, 前端每次整段回传. 每轮只对末条 user
      重做检索 + 渲染 RAG 上下文, 之前轮次原样回放 (详见
      :func:`bapee.chatbot.bpae_pipeline.chat_bot`).
    """
    started = time.monotonic()
    log_ctx = _chat_log_ctx(request, stream)
    logger.info("chat.start", extra=log_ctx)

    history_dicts = [m.model_dump() for m in request.messages]

    if not stream:
        try:
            text = await _with_timeout(
                chat_bot_text(
                    route_url=request.route_url,
                    payload=request.payload,
                    history=history_dicts,
                    model=request.model,
                ),
                ctx_name="chat",
                log_ctx=log_ctx,
                started=started,
            )
        except Exception as exc:
            if not isinstance(exc, HTTPException):
                logger.exception(
                    "chat.error",
                    extra={
                        **log_ctx,
                        "outcome": "error",
                        "latency_ms": _ms_since(started),
                        "exc_type": type(exc).__name__,
                    },
                )
            raise
        logger.info(
            "chat.done",
            extra={
                **log_ctx,
                "outcome": "ok",
                "latency_ms": _ms_since(started),
                "total_chars": len(text),
            },
        )
        return PlainTextResponse(text)

    upstream = chat_bot(
        route_url=request.route_url,
        payload=request.payload,
        history=history_dicts,
        model=request.model,
    )
    return StreamingResponse(
        _logged_stream("chat", log_ctx, started, upstream, http_request),
        media_type="text/event-stream",
        headers={
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )

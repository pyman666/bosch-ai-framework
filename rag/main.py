"""FastAPI 应用装配点 — 路由 / lifespan / 中间件 / 异常处理 都在这里搭起来.

启动流程 (FastAPI ``lifespan``):

1. 应用启动, gunicorn / uvicorn fork 出 worker;
2. :func:`lifespan` 被调一次, 内部触发 :func:`bapee.chatbot.bpae_pipeline.init`
   — 真正去加载 sentence-transformers / FAISS / AST jsonl, 失败直接 raise,
   worker 起不来, gunicorn master 看见就重试 (并发 worker 0 时整个 deploy
   fail, BTP / k8s 不会把流量切过来);
3. 一切就绪, ``yield`` 出来, FastAPI 开始接请求;
4. 收到 SIGTERM, 回到 ``yield`` 之后, 这里目前没东西要释放, 直接退出.

历史: 之前 ``bapee.chatbot.bpae_pipeline`` 在模块级直接构造 pipeline, 任何
``import`` (pytest / mypy / 文档生成) 都会触发加载, 启动失败的 traceback 还
埋在 import 链里. lifespan 重构之后启动失败一目了然, 测试也能在不加载 KB
的前提下 import 整个项目.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from rag.chatbot import bpae_pipeline
from rag.chatbot.routes import router as bot_router
from infra.observability import RequestIDMiddleware, get_request_id
from rag.core.ratelimit import RateLimitMiddleware, build_rate_limiter
from infra.utils import exception_detail
from rag.settings import (
    RATE_LIMIT_BURST,
    RATE_LIMIT_PER_MIN,
    REDIS_SSL_CERT_REQS,
    REDIS_URL,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期 hook — 启动期同步构 pipeline, 失败就让 worker 退出."""
    # 加载 infra LLM 配置 (模型列表, Router 参数, 可选 Redis backend)
    from infra.settings import load_config
    load_config(redis_url=REDIS_URL)

    logger.info("startup: building BPAE pipeline...")
    try:
        # 走 ``bpae_pipeline.init`` (而不是 ``from ... import init``) 这样测试
        # 用 ``monkeypatch.setattr(bpae_pipeline, "init", ...)`` 替换才有效.
        bpae_pipeline.init()
    except Exception:
        # 用 exception 是为了把 traceback 完整记到日志里; 它仍然会 re-raise
        # 中断 lifespan, worker 起不来 — 部署平台看到的不只是日志, 还有
        # 退出码 / 健康检查失败信号.
        logger.exception("startup: pipeline build failed, aborting")
        raise

    logger.info("startup: pipeline ready, accepting traffic")
    try:
        yield
    finally:
        # 通知所有 in-flight SSE 流: server 要 shut down 了, 优雅收尾发个
        # ``event: shutdown`` 让前端能识别而非以为是网络断. 跑完后 gunicorn
        # ``graceful_timeout`` 才开始倒计时砍剩下没收尾的 worker.
        logger.info("shutdown: signaling in-flight streams")
        shutdown_event.set()
        logger.info("shutdown: bye")


# 模块级 shutdown signal — SSE 流每片 check 一下, 命中就优雅收尾. 在 lifespan
# 的 cleanup 阶段 set; 进程级单例足够 (一个 gunicorn worker 一个 event loop,
# 一份 event 就够全 worker 内所有 task 用了). 见 :mod:`bapee.chatbot.routes`
# 的 ``_logged_stream`` 消费侧.
shutdown_event = asyncio.Event()


_app_description = """
## ChatBot
FastAPI project, 单 pipeline, 定位为 BPAE / O2C 的**报错 / data 结果分析助手**.
通用 RAG 实现在 `bapee/rag/`, BPAE 业务定制 (URL 表 / payload 字段 / prompt
措辞) 在 `bapee/chatbot/bpae_pipeline.py`.

- `POST /bot/ask` — **单轮**报错 / data 诊断. 不带对话历史. 当前实现是 hybrid 检索
  (deterministic lookup + BM25 + dense FAISS, 可选 cross-encoder rerank) → LLM 作答.
  默认 SSE 流式 (`?stream=false` 可退回整段 `text/plain`).
- `POST /bot/chat` — **多轮** chat 版. 协议跟 `/ask` 完全一致, 多带一个 `messages`
  字段 (前端持有完整对话历史, 末条 user). 服务端 stateless, 每轮只对末条 user
  重新检索 + 渲染.
- `GET /livez` — Liveness probe. 永远 200 (进程响应即活着). k8s ``livenessProbe``
  / BTP "重启决策" 指这里. 失败 = 重启 pod.
- `GET /readyz` — Readiness probe. pipeline 就绪才 200, 启动期返 503. k8s
  ``readinessProbe`` / BTP gorouter "切流量决策" 指这里. 失败 = 摘出 LB pool 不重启.
- `GET /healthz` — `/readyz` 的兼容 alias, 保留给现有 `manifest.yml`. 新部署直接用 `/readyz` 更语义化.

典型入口: 业务客户在前端点某条数据, 前端把"路由 URL + data id + 当前状态文本"
打包进 query 调本接口, LLM 给出客户向的诊断 + 下一步.

通过 LiteLLM Router 调底层模型 (多 provider / 多 key 自动路由 + 限流 + 重试).
所有请求都会在响应头里回写 `X-Request-ID`, 客户端报障时把这个 ID 给到运维就
能快速 grep 出全链路日志.

### Notes
All endpoints are protected via basic authentication. `/livez` / `/readyz` /
`/healthz` 都是例外 — 不走 auth, 探活 / 就绪检查不能依赖业务密钥, 也不该让
鉴权故障把健康检查带挂.
"""


app = FastAPI(
    title="LLM API",
    description=_app_description,
    version="0.1.0",
    license_info={"name": "Learning Purposes Only"},
    contact={
        "name": "HN",
        "email": "hn_1992@163.com",
    },
    openapi_tags=[
        {"name": "ChatBot", "description": "Chat with your documents"},
    ],
    swagger_ui_parameters={"defaultModelsExpandDepth": -1},
    lifespan=lifespan,
)

# 中间件挂载顺序很关键 — Starlette 是 "后挂的先生效" (LIFO):
#
#   add 顺序:        RateLimit (先)  →  RequestID (后)
#   请求 inbound:    RequestID  →  RateLimit  →  routes
#   响应 outbound:   routes     →  RateLimit  →  RequestID
#
# 这样:
# - RequestID 是最外层, 任何请求 (含被限流的 429) 都先拿到 X-Request-ID,
#   contextvar 已经 set 进去了;
# - RateLimit 内层在 contextvar 已有的前提下检查, 触发 429 时 body 里能塞
#   request_id, 响应再回到 RequestID 那层时它把响应头 X-Request-ID 贴上;
# - 顺序反过来 (RequestID 在内) 会导致 429 响应的 request_id 总是 "-".
#
# 加新中间件 (CORS / GZip / auth) 时同样想清"是想跑在限流前还是后":
#   - 想被限流 (e.g. CORS preflight 不该消耗 token bucket) → 挂在 RateLimit
#     之后 = add 顺序排 RateLimit 之前;
#   - 想跑在限流前 (e.g. GZip 压响应) → 挂在 RateLimit 之前 = add 顺序排
#     RateLimit 之后.
# Backend 自动选: 有 REDIS_URL (env 或 BTP service binding) → Redis 版,
# 跨 worker / instance 精确限流; 没有 → in-memory 版, 多 worker 下倍数放
# 宽但单进程内仍准确, 应用照样起. 详见 ``bapee.core.ratelimit.build_rate_limiter``.
_rate_limiter = build_rate_limiter(
    rate_per_min=RATE_LIMIT_PER_MIN,
    burst=RATE_LIMIT_BURST,
    redis_url=REDIS_URL,
    redis_ssl_cert_reqs=REDIS_SSL_CERT_REQS,
)
app.add_middleware(
    RateLimitMiddleware,
    paths=("/bot/",),
    limiter=_rate_limiter,
)
app.add_middleware(RequestIDMiddleware)

app.include_router(bot_router, prefix="/bot", tags=["ChatBot"])


# ---------------------------------------------------------------------------
# Health checks — liveness (活) / readiness (就绪) / healthz (兼容 alias)
#
# 三个 endpoint 语义不同, 部署平台用法不同:
#
#   /livez   - "进程还活着, 不要 kill 我". 永远 200 (除非进程已死, 此时根本
#              不会响应). k8s ``livenessProbe`` 应指这里; 失败 = 重启 pod.
#   /readyz  - "可以接客户流量了". pipeline 就绪才 200, 启动期 / 出问题返
#              503. k8s ``readinessProbe`` / BTP gorouter readiness 应指这里;
#              503 = 把这个实例摘掉但不重启.
#   /healthz - ``/readyz`` 的别名, 保留是因为 manifest.yml 历史指它了.
#              想新部署时直接用 ``/readyz`` 更语义化, 此 alias 保持兼容.
#
# 拆开后真正受益的场景: 启动慢的 instance (sentence-transformers 加载 ~30s)
# 期间 BTP / k8s 看到 livez 200 + readyz 503, 不会误判为"死掉" 触发重启, 而
# 是耐心等 readiness ready. 单 ``/healthz`` 二合一时 liveness 也是 503,
# 平台可能直接重启进入死循环.
# ---------------------------------------------------------------------------

def _readyz_payload() -> tuple[int, dict[str, Any]]:
    """构造 readiness 响应 — 拆出来给 ``/readyz`` 和 ``/healthz`` 共用."""
    ready = bpae_pipeline.is_ready()
    return (
        200 if ready else 503,
        {"status": "ok" if ready else "starting", "ready": ready},
    )


@app.get("/livez", include_in_schema=False)
async def livez() -> JSONResponse:
    """Liveness probe — 进程响应即 200. 用于"是否该 kill / 重启"决策.

    永远 200 (能跑到这一步说明进程活着且 event loop 没卡死). 拿不到响应
    时调用方应该认为进程死了, 不是这个 endpoint 返了非 2xx — 这是 liveness
    跟 readiness 在语义上最关键的区别.
    """
    return JSONResponse(status_code=200, content={"status": "alive"})


@app.get("/readyz", include_in_schema=False)
async def readyz() -> JSONResponse:
    """Readiness probe — pipeline 就绪才 200, 否则 503. 用于"是否切流量"决策.

    启动期 (lifespan 还在加载 sentence-transformers / FAISS) 返 503; lifespan
    成功跑完 ``init()`` 之后返 200. 部署平台看到 503 应该把这个 instance
    暂时摘出 LB pool, **不要重启** — 重启反而让冷加载再来一次, 加剧抖动.
    """
    status_code, content = _readyz_payload()
    return JSONResponse(status_code=status_code, content=content)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Backward-compat alias of ``/readyz``.

    历史: ``manifest.yml`` 的 ``health-check-http-endpoint`` 设的是 ``/healthz``,
    保留这个路径让现有部署零改动. 新部署 / 文档建议直接用 ``/readyz``,
    语义更清晰.
    """
    status_code, content = _readyz_payload()
    return JSONResponse(status_code=status_code, content=content)


# ---------------------------------------------------------------------------
# Exception handlers — 客户友好 + 不漏内部信息
# ---------------------------------------------------------------------------

async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """4xx / 显式抛的 ``HTTPException`` — detail 本就给客户端看, 原样回 +
    带上 ``request_id`` 方便客户报障关联日志.

    覆盖范围: ``fastapi.HTTPException`` (它继承自 starlette 那个), 含我们
    在 :mod:`bapee.chatbot.routes` 里抛的 401 / 413 / 422, 以及 FastAPI 自
    己抛的 404 (path 不存在) / 405 (method 不对) 等. ``5xx`` 的 HTTPException
    虽然罕见 (我们不主动抛), 这里也会原样回 — 业务代码主动抛 5xx 意味着这
    个 detail 是写过 review 的, 不藏.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": jsonable_encoder(exception_detail(exc)),
            "request_id": get_request_id(),
        },
        headers=getattr(exc, "headers", None),
    )


async def _request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Pydantic 拒绝请求体 → 422. 字段级错误明细要回给客户端, 不然前端不
    知道哪一项填错; 同时带 ``request_id``."""
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(exc.errors()),
            "request_id": get_request_id(),
        },
    )


async def _server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """5xx / 未分类异常 — 完整栈进日志, 客户端只看到一句通用消息 + request_id.

    动机: 之前 detail 里直接 ``str(exc)``, OpenAI 报错文本 / 内部文件路径 /
    底层栈帧都可能被前端原样展示甚至贴进客户邮件. 改成:
      - server 端 :meth:`logger.exception` 记完整 stack;
      - 客户端只拿 ``"internal server error"`` + ``request_id``, 客户拿 ID
        来问运维, 由运维去日志里查实际原因.

    ``ResponseValidationError`` (我们自己声明的 schema 跟实际响应对不上)
    走的也是这里 — 那是 server 端的 bug, 客户端拿到 detail 没意义.
    """
    rid = get_request_id()
    logger.exception(
        "unhandled exception in request",
        extra={
            "path": request.url.path,
            "method": request.method,
            "exc_type": type(exc).__name__,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "detail": "internal server error",
            "request_id": rid,
        },
    )


# 注意: FastAPI 默认会给 ``HTTPException`` / ``RequestValidationError`` 注册
# 处理器, 我们覆盖它们是为了统一加 ``request_id`` 字段. 顺序无关 (按异常类
# 型最具体匹配), 写在最后让阅读时一眼看到 catchall.
app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
app.add_exception_handler(HTTPException, _http_exception_handler)
app.add_exception_handler(RequestValidationError, _request_validation_handler)
app.add_exception_handler(ResponseValidationError, _server_error_handler)
app.add_exception_handler(Exception, _server_error_handler)

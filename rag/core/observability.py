"""集中放可观测性 (logging / tracing) 基础设施.

包含三块, 互相耦合较深, 放一起方便看全:

1. :data:`request_id_var` — :mod:`contextvars` 上下文变量, 每个请求一份.
2. :class:`RequestIDMiddleware` — Starlette 中间件, 给每个进入的请求生成 (或
   读取客户端传的) ``X-Request-ID``, 写进 ``request_id_var``, 响应再原样回写.
3. :class:`JsonFormatter` — 一行一个 JSON, 自动注入 request 追踪 + ALS Kibana
   兼容字段, 以及日志调用方传进来的所有 ``extra={...}`` 字段.

其他模块只用 ``logger.info("msg", extra={"k": v})`` 就够, 中间件 / formatter
对业务代码透明; 日志聚合平台 (Splunk / Datadog / Loki / BTP ALS) 拿到追踪
ID 就能把客户端报障 → 服务端日志一条线串起来.

## SAP BTP Application Logging Service (ALS) 字段约定

ALS 自带的 Kibana view 默认按这套字段渲染 (来自 sap-cf-logging 协议):

- ``written_at``    — ISO 8601 UTC 时间, 带 ``Z`` 后缀
- ``correlation_id`` — 跨服务追踪 ID (我们用 request_id 同源)
- ``level``         — 日志级别
- ``msg``           — 日志正文
- ``logger`` / ``type`` — 来源 logger / 日志类型

我们的 formatter 同时输出两套字段名 (``timestamp``+``written_at``,
``request_id``+``correlation_id``, ``message``+``msg``), 让 BTP ALS Kibana
view 跟非 BTP 环境的通用聚合工具 (Splunk / ELK / Loki) 都能开箱即用. 重复
几个字段每条日志多 ~50 字节, 量级可忽略.

历史: 启动期日志和访问日志在 :mod:`gunicorn_conf` 里搭好了 JSON 输出, 但是
formatter 只 dump 4 个固定字段, 业务调用 ``logger.info(..., extra={...})``
塞的字段都被吞了, 排查时还得 ssh 进 worker 翻原始 stderr. 这次把 formatter
扩成 "标准字段 + ALS 别名 + extra + request_id + exc_info" 四件套, 同时新增
请求 ID 机制, 让一条客户请求对应的所有 server 端日志在聚合后能精确串成时间线.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


# ---------------------------------------------------------------------------
# Request ID context — 一个请求一份, 通过 contextvars 让同一 task 内任意深度
# 的 logger.info(...) 都能拿到; async 边界 (await / asyncio.create_task 通过
# ``copy_context``) 都会正确继承.
# ---------------------------------------------------------------------------

REQUEST_ID_HEADER = "X-Request-ID"

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)


def get_request_id() -> str:
    """同进程任意位置取当前请求 ID; 没有请求上下文时返回 ``'-'``."""
    return request_id_var.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """给每个进入的 HTTP 请求生成 / 透传 ``X-Request-ID``.

    优先用客户端传的 ``X-Request-ID`` (上游 LB / API gateway / 调用方常会带);
    没有就 ``uuid4`` 生成一个. 响应头原样回写, 客户端拿到这个 ID 报障时
    我们能用它 grep 出全链路日志.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        token = request_id_var.set(rid)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = rid
        return response


# ---------------------------------------------------------------------------
# JSON formatter — 把 LogRecord 序列化成单行 JSON, 自动包含 request_id 和
# 业务 extra 字段.
# ---------------------------------------------------------------------------

# Python ``LogRecord`` 自带的属性, 我们不想原样 dump 到 JSON. 这份列表抄自
# CPython 3.12 ``logging.LogRecord.__init__`` + 几个 ``Formatter.format`` 期
# 间塞的 (``message`` / ``asctime``); 业务 ``extra={...}`` 的键被禁止与这里
# 重名 (Python logging 自己会先 reject), 所以差集就是干净的 extra 字段.
_LOGRECORD_RESERVED: frozenset[str] = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno",
        "pathname", "filename", "module",
        "exc_info", "exc_text", "stack_info",
        "lineno", "funcName",
        "created", "msecs", "relativeCreated",
        "thread", "threadName",
        "processName", "process",
        "message", "asctime",
        "taskName",
    }
)


def _iso_utc_from_record(record: logging.LogRecord) -> str:
    """LogRecord 的创建时间转 ISO 8601 UTC 带毫秒 + ``Z`` 后缀.

    形如 ``"2026-05-14T07:14:33.812Z"``. BTP ALS 的 Kibana view 期望这个
    格式, 用 ``Z`` 而不是 ``+00:00`` 是 sap-cf-logging 历史约定.
    """
    dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(record.msecs):03d}Z"


class JsonFormatter(logging.Formatter):
    """单行 JSON formatter, 自动注入追踪 ID + ALS 别名 + ``extra={...}`` 字段.

    输出形如 (生产 + BTP ALS 友好)::

        {"timestamp": "2026-05-14T07:14:33.812Z",
         "written_at": "2026-05-14T07:14:33.812Z",    # ALS 别名
         "level": "INFO",
         "logger": "bapee.chatbot.routes", "type": "log",
         "message": "ask.start", "msg": "ask.start",   # ALS 别名
         "request_id": "abc123",
         "correlation_id": "abc123",                    # ALS 别名
         "route_url": "/jit/...", "model": "gpt-4"}     # 业务 extra

    业务侧只需 ``logger.info("ask.start", extra={"route_url": ..., "model": ...})``
    任何 key (除了 :data:`_LOGRECORD_RESERVED` 这些 logging 保留字) 都会原
    样落到 JSON 顶层. exception (``logger.exception`` / ``exc_info=True``)
    会以多行 traceback 字符串塞到 ``exception`` 字段.

    重复字段开销: 每条日志 ~50B 重复 (``written_at`` / ``msg`` / ``correlation_id``);
    一天 100k 条日志增量 ~5MB, 量级可忽略.
    """

    def format(self, record: logging.LogRecord) -> str:
        ts = _iso_utc_from_record(record)
        rid = request_id_var.get()
        msg = record.getMessage()
        payload: dict[str, Any] = {
            # 主字段 — 通用工具 (Splunk / ELK / Loki) 习惯用这套
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
            "request_id": rid,
            # ALS / sap-cf-logging 别名 — 给 BTP Kibana view 用
            "written_at": ts,
            "msg": msg,
            "correlation_id": rid,
            "type": "log",
        }
        for key, val in record.__dict__.items():
            if key in _LOGRECORD_RESERVED or key.startswith("_"):
                continue
            # 跳过 formatter 已写的字段, 避免 extra 误覆盖. 业务用相同 key
            # 命名 (e.g. ``extra={"message": ...}``) 是误用, 静默忽略而非
            # 默默覆盖追踪信息.
            if key in payload:
                continue
            payload[key] = val
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Logging setup helper — 给 run.py (本地 dev) 和 gunicorn_conf.py (生产) 共用,
# 避免两边手抖配置漂移.
# ---------------------------------------------------------------------------

def setup_basic_logging(level: int = logging.INFO) -> None:
    """配置 root logger 输出 JSON 到 stdout — 适合本地 / 单进程场景.

    生产 (gunicorn) 走 :mod:`gunicorn_conf` 里的 QueueListener 多 worker
    路径, 不要调这个; 本地 ``python run.py`` 没有 gunicorn 加持, 调一下就够.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

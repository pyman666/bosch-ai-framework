"""Gunicorn 配置 — JSON logging + QueueHandler + 生命周期 hooks，所有 agent 共用。

用法::

    cd forecast && gunicorn forecast.main:app -c ../infra/logs.py
    cd analytics && gunicorn analytics.main:app -c ../infra/logs.py
    cd document && gunicorn document.main:app -c ../infra/logs.py
    cd rag && gunicorn rag.main:app -c ../infra/logs.py
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import sys

# ---- stdout 编码修复 ----
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# ---- Binding ----
bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = int(os.environ.get("GUNICORN_WORKERS", str(os.cpu_count() or 2)))
worker_class = "uvicorn.workers.UvicornWorker"

os.environ.setdefault("WEB_CONCURRENCY", str(workers))

# ---- Performance & Stability ----
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))  # LLM calls can be slow
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# ---- Worker Health ----
max_requests = int(os.environ.get("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = 50  # randomize to avoid thundering herd
preload_app = os.environ.get("GUNICORN_PRELOAD_APP", "true").lower() == "true"

# ---- Logging ----
accesslog = "-"
errorlog = "-"
loglevel = "info"


class JsonFormatter(logging.Formatter):
    """JSON 格式日志, 适合 ELK / Cloud Logging."""

    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Include exception info if present
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        if hasattr(record, "request_id"):
            log_record["request_id"] = record.request_id
        return json.dumps(log_record, ensure_ascii=False)


sync_handler = logging.StreamHandler(sys.stdout)
sync_handler.setLevel(logging.INFO)
sync_handler.setFormatter(JsonFormatter())

log_queue = queue.Queue(-1)
queue_handler = logging.handlers.QueueHandler(log_queue)


access_logger = logging.getLogger("gunicorn.access")
access_logger.setLevel(logging.INFO)
access_logger.handlers = []
access_logger.addHandler(queue_handler)

error_logger = logging.getLogger("gunicorn.error")
error_logger.setLevel(logging.ERROR)
error_logger.handlers = []
error_logger.addHandler(queue_handler)

uvicorn_logger = logging.getLogger("uvicorn.access")
uvicorn_logger.setLevel(logging.INFO)
uvicorn_logger.handlers = access_logger.handlers

uvicorn_error_logger = logging.getLogger("uvicorn.error")
uvicorn_error_logger.setLevel(logging.ERROR)
uvicorn_error_logger.handlers = error_logger.handlers


root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [queue_handler]


# ---- Lifecycle Hooks ----

def post_fork(server, worker):
    """Start log queue listener in each worker."""
    listener = logging.handlers.QueueListener(
        log_queue,
        sync_handler,
        respect_handler_level=True,
    )
    listener.start()
    worker.log_listener = listener
    server.log.info(f"Worker {worker.pid} booted, listening on {bind}")


def worker_exit(server, worker):
    """Stop log queue listener on worker exit."""
    listener = getattr(worker, "log_listener", None)
    if listener:
        try:
            listener.stop()
        except Exception:
            pass


def on_exit(server):
    """Log final shutdown."""
    server.log.info("Gunicorn master shutting down.")

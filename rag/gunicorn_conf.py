import logging
import logging.handlers
import os
import queue
import sys

# JsonFormatter 从应用包里取 — 这样 dev (run.py) 和 prod (gunicorn) 用同
# 一个 formatter, 字段 / request_id 注入完全一致.
from rag.core.observability import JsonFormatter

# BTP / Cloud Foundry 通过 ``PORT`` env var 告知应用该监听哪个端口 — 实际值
# 一般也是 8080, 但理论上动态分配, 写死会在非默认配置 BTP 实例上失败.
# 本地 dev 没设这个 env 时回退到 8080 (跟 run.py 一致).
_port = os.environ.get("PORT", "8080")
bind = f"0.0.0.0:{_port}"

workers = 2
worker_class = 'uvicorn.workers.UvicornWorker'

# 把 workers 数暴露给应用代码 (用于检测多 worker 与单进程内存任务表的冲突)
os.environ.setdefault("WEB_CONCURRENCY", str(workers))

# ---- Timeouts ---------------------------------------------------------------
# 我们的 SSE 流可能跑几十秒 (LLM 长回答), 必须放宽 gunicorn 默认 30s 心跳超时:
#
# - ``timeout``: gunicorn master 给 worker 的 "n 秒没心跳就 kill" deadline.
#   uvicorn worker 是 async, 自己会发心跳, 但 LLM 调用阻塞那段没心跳, 长
#   stream 容易被误杀. 拉到 180s, 比 ``STREAM_MAX_DURATION_SEC`` (120s) 还
#   长, 给"流刚结束还在收尾"留缓冲.
# - ``graceful_timeout``: worker 收到 SIGTERM 后, master 等多久才发 SIGKILL.
#   BTP ``--app-stop-timeout`` 默认 30s (滚动发布期), 我们设 25s, 留 5s 给
#   CF 自己的 SIGKILL→reschedule 过程, 比 BTP 略短才能"自然退出而非被砍".
#   注意: 在我们这 25s 内, 短请求能跑完, 长 SSE 流大概率被切. 真要"零中断
#   滚动发布"得在应用层加 shutdown signal 协调 — 留作下一波.
# - ``keepalive``: HTTP keep-alive 连接的空闲容忍. BTP Gorouter 默认会复用
#   后端连接, 拉到 75s 跟 nginx / ALB 习惯对齐.
timeout = 180
graceful_timeout = 25
keepalive = 75

accesslog = "-"
errorlog = "-"
loglevel = 'info'


sync_handler = logging.StreamHandler(sys.stdout)
sync_handler.setLevel(logging.INFO)
sync_handler.setFormatter(JsonFormatter())

log_queue = queue.Queue(-1)  # infinite size
queue_handler = logging.handlers.QueueHandler(log_queue)


access_logger = logging.getLogger("gunicorn.access")
access_logger.setLevel(logging.INFO)
access_logger.handlers = []  # remove any pre-existing handlers
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


def post_fork(server, worker):
    """
    Start QueueListener inside worker process.
    Ensures logs are serialized and not interleaved.
    """
    listener = logging.handlers.QueueListener(
        log_queue,
        sync_handler,
        respect_handler_level=True,
    )
    listener.start()
    worker.log_listener = listener


def worker_exit(server, worker):
    """
    Stop the QueueListener gracefully.
    """
    listener = getattr(worker, "log_listener", None)
    if listener:
        try:
            listener.stop()
        except Exception:
            pass

import logging
import logging.handlers
import os
import queue
import sys
import json

bind = '0.0.0.0:8080'
workers = 2
worker_class = 'uvicorn.workers.UvicornWorker'

# 把 workers 数暴露给应用代码 (用于检测多 worker 与单进程内存任务表的冲突)
os.environ.setdefault("WEB_CONCURRENCY", str(workers))

accesslog = "-"
errorlog = "-"
loglevel = 'info'


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(log_record)


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

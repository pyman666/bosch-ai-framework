"""本地 dev 入口 — ``python run.py`` 就起服务.

跟生产 (gunicorn) 路径的差异:

- 单 worker, 单进程, 不 fork, 方便断点;
- 用 :func:`rag.core.observability.setup_basic_logging` 配 root logger 输出 JSON
  到 stdout — 跟生产一致, 这样开发期看到的日志结构跟线上排查时一样, 不会
  出现"本地一切正常 / 线上日志缺字段"的尴尬.

生产部署走 ``gunicorn -c gunicorn_conf.py rag.main:app``, 见 ``manifest.yml``.
"""
if __name__ == "__main__":
    import logging

    import uvicorn

    from rag.core.observability import setup_basic_logging

    setup_basic_logging(level=logging.DEBUG)

    uvicorn.run(
        "rag.main:app",
        host="127.0.0.1",
        port=8080,
        log_level="debug",
        log_config=None,
    )

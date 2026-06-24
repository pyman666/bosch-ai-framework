"""通用基础设施 — 业务无关, 可复用到其他项目.

这一层提供整套 "FastAPI + LLM + BTP" 应用骨架, 不依赖任何业务知识:

- :mod:`bapee.core.auth`          — XSUAA JWT / Basic Auth 自动 dispatcher
- :mod:`bapee.core.btp`           — VCAP_SERVICES 解析 + Redis URL 解析
- :mod:`bapee.core.llm`           — LiteLLM Router 工厂 + AI Core 集成 (含 Redis 共享 backend)
- :mod:`bapee.core.observability` — JSON logging + RequestID middleware (ALS 兼容)
- :mod:`bapee.core.ratelimit`     — Token bucket rate limiter (in-memory + Redis 自动选)
- :mod:`bapee.core.utils`         — 通用小工具 (exception_detail 等)

依赖规则:

- ``core/`` 内部模块**可以**互相 import (e.g. ``ratelimit`` 用 ``observability``,
  ``auth`` 用 ``btp``).
- ``core/`` **不得** import :mod:`bapee.chatbot` / :mod:`bapee.rag` / 任何项目业务,
  也**不应** import :mod:`bapee.settings` 里的项目特定值 (``SYSTEM_PROMPT`` 等);
  只能读跨项目通用的字段 (BOT_KEY / RATE_LIMIT_PER_MIN 这种).

放在 ``bapee.core`` 是为了让 ``bapee/`` top-level 只剩项目层概念: ``server``
(入口) + ``settings`` (项目配置) + ``chatbot/`` (业务应用) + ``rag/`` (RAG 库)
+ ``core/`` (基础设施). 想加新 bot (e.g. ``vlmbot/``) 时跟 ``chatbot/`` 平级,
共享 ``core/`` + ``rag/``, 互不污染.
"""

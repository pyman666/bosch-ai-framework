"""RAG 领域特有基础设施 — 仅 rag agent 使用.

- :mod:`rag.core.ratelimit` — Token bucket rate limiter (in-memory + Redis)

通用基础设施 (LLM, Auth, Settings, Logging, BTP) 已统一走 ``infra/``.
"""

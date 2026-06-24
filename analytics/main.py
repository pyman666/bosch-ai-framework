"""ABI — AI BI Gateway 入口."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from analytics.api.health import router as health_router
from analytics.api.chat import router as chat_router

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期: 启动时预热 LLM Router, 关闭时清理."""
    # 启动时预热 — 触发 Router 初始化, 提前暴露配置错误
    from infra.llm import get_router

    get_router()
    log.info("ABI gateway started, LLM router ready")
    yield
    log.info("ABI gateway shutting down")


app = FastAPI(
    title="ABI — AI BI Gateway",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 健康检查不挂鉴权
app.include_router(health_router, tags=["health"])

# 业务路由（后续挂 auth dependency）
app.include_router(chat_router, prefix="/api", tags=["chat"])
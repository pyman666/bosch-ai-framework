"""fcst — 预测 AI Agent 后端服务 (FastAPI)。"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from infra.auth import require_auth
from forecast.database import get_db, init_db, SessionLocal
from forecast.core.skill_manager import seed_preset_skills
from forecast.core.rate_limit import RateLimitMiddleware
from forecast.routes import chat, skill, forecast, assistant


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    db = SessionLocal()
    try:
        seed_preset_skills(db)
    finally:
        db.close()

    from infra.task import cleanup_expired_tasks
    from forecast.routes.assistant import _cleanup_expired as assistant_cleanup

    async def _combined_cleanup():
        await cleanup_expired_tasks()
        assistant_cleanup()

    cleanup_task = asyncio.create_task(_task_cleanup_loop(_combined_cleanup))

    yield

    cleanup_task.cancel()


async def _task_cleanup_loop(cleanup_fn, interval: float = 300):
    """每 5 分钟清理一次过期任务."""
    while True:
        await asyncio.sleep(interval)
        await cleanup_fn()


app = FastAPI(
    title="fcst — Forecast AI Agent",
    description="AI-assisted forecast formula designer — chat with an agent "
                "to build prediction skills (DSL or Python).",
    lifespan=lifespan,
    version="0.1.0",
    license_info={"name": "Learning Purposes Only"},
    contact={
        "name": "HN",
        "email": "hn_1992@163.com",
    },
)

app.add_middleware(RateLimitMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(chat.router, dependencies=[Depends(require_auth)])
app.include_router(skill.router, dependencies=[Depends(require_auth)])
app.include_router(forecast.router, dependencies=[Depends(require_auth)])
app.include_router(assistant.router, dependencies=[Depends(require_auth)])


@app.get("/mock", response_class=HTMLResponse)
def mock():
    mock_path = Path(__file__).parent.parent / "mock.html"
    return mock_path.read_text(encoding="utf-8")


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok", "service": "fcst"}
    except Exception as e:
        logging.getLogger(__name__).exception("Health check failed")
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})

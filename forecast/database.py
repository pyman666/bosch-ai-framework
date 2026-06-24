"""SQLite 数据库层 — SQLAlchemy 引擎 + 会话工厂。"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database location
# ---------------------------------------------------------------------------

DB_DIR = Path(__file__).parent.parent / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = f"sqlite:///{DB_DIR / 'fcst.db'}"

# ---------------------------------------------------------------------------
# Engine & session (synchronous — SQLite + async don't mix well)
# ---------------------------------------------------------------------------

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_size=20,
    max_overflow=10,
    pool_timeout=10,
    echo=False,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, _connection_record):
    """每次连接时启用 WAL 模式、外键约束和 busy_timeout。"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")  # 5s wait on DB lock
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    """FastAPI 依赖注入 — 返回数据库会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """创建所有不存在的数据库表。"""
    Base.metadata.create_all(bind=engine)


def reset_db():
    """删除并重建所有数据库表 — 开发期专用，生产环境勿用。

    用法：在 Python REPL 或一次性脚本中调用 ``reset_db()``，
    或设置环境变量 ``FCST_RESET_DB=1`` 后启动服务自动执行。
    """
    db_path = DB_DIR / "fcst.db"
    backup = None
    if db_path.exists():
        backup = db_path.with_suffix(".db.bak")
        db_path.rename(backup)

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    log.warning("Database reset complete — all tables dropped and recreated")

    if backup and backup.exists():
        backup.unlink()
        log.info("Old database backup removed: %s", backup)

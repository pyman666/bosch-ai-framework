"""Alembic 环境配置 — SQLite 迁移。"""

import os
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# Alembic Config 对象
config = context.config

# 日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 动态设置数据库 URL（优先使用环境变量，否则使用默认路径）
db_path = os.environ.get("FCST_DB_PATH")
if not db_path:
    db_path = str(Path(__file__).parent.parent / "data" / "fcst.db")
config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")

# 导入 Base 和所有 ORM 模型
from forecast.database import Base  # noqa: E402
from fcst import db_models  # noqa: E402, F401

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式迁移（生成 SQL 脚本）"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite 需要 batch mode 支持 ALTER TABLE
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式迁移（直接连接数据库）"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite 需要 batch mode
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

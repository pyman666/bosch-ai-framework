"""PDF 客户注册表 + ``support()`` 声明入口。

与 Excel 侧 ``apdfi.excel.clients`` 一脉相承: 每个客户子包调一次 ``support()``
声明自己, ``pdf/routes.py`` 通过 pkgutil 自动发现子包并调 ``register_all(router)``
一次性挂上路由. 接入新 PDF 客户不需要修改任何现有文件.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel

# label -> (schema, schema_label)
_REGISTRY: dict[str, tuple[type, str]] = {}


def support(
    schema: "type[BaseModel]",
    *,
    label: str,
    schema_label: str,
) -> None:
    """PDF 客户接入声明入口。

    Args:
        schema: VLM 抽取结果的 pydantic 类型.
        label: URL-safe 标识 (e.g. ``"retro"``).
        schema_label: 给前端 ACK ``schema_label`` 字段用的可读名称.
    """
    _REGISTRY[label] = (schema, schema_label)


def get_config(label: str) -> tuple[type, str] | None:
    """按 label 查询已注册 PDF 客户. 返回 ``(schema, schema_label)`` 或 ``None``."""
    return _REGISTRY.get(label)


def all_labels() -> list[str]:
    """返回所有已注册 PDF 客户 label 列表."""
    return list(_REGISTRY)

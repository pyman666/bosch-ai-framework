"""Excel 客户注册表 + ``support()`` 声明入口.

每个客户子包的 ``__init__.py`` 只需调一次 ``support(config)`` 声明自己.
``routes.py`` 通过 pkgutil 自动发现所有子包, 然后通过统一的参数化端点
(``POST /excel?client=<label>``) 派发到正确的客户处理逻辑.
接入新客户**不需要修改任何其它文件**.
"""
from __future__ import annotations

from typing import Any

from ..simple import SimpleExcelConfig
from ..wide import WideExcelConfig
from ..complex import ComplexExcelConfig

# label -> (engine, config, kwargs)
_REGISTRY: dict[str, tuple[str, Any, dict]] = {}


def support(
    config: SimpleExcelConfig | WideExcelConfig | ComplexExcelConfig,
    **kwargs,
) -> None:
    """客户接入声明入口.

    Args:
        config: 客户配置对象.
        **kwargs: 仅 ``SimpleExcelConfig`` 需要额外传 ``path_prefix="/<name>"``
            (作为 label 来源, 因为 SimpleExcelConfig 本身没有 label 字段);
            ``WideExcelConfig`` / ``ComplexExcelConfig`` 的 label 从 ``config.label``
            自动推导.
    """
    if isinstance(config, SimpleExcelConfig):
        engine = "simple"
        path_prefix = kwargs.get("path_prefix", "")
        label = path_prefix.lstrip("/")
        if not label:
            raise ValueError("SimpleExcelConfig 的 support() 必须传 path_prefix='/<name>'")
    elif isinstance(config, WideExcelConfig):
        engine = "wide"
        label = config.label
    elif isinstance(config, ComplexExcelConfig):
        engine = "complex"
        label = config.label
        # complex 客户需要注册 ChatHandler, 路由才能通过 handler_name 找到它
        from ..complex import register_chat_handler

        register_chat_handler(config)
    else:
        raise TypeError(f"support() 不支持的 config 类型: {type(config).__name__}")
    _REGISTRY[label] = (engine, config, kwargs)


def get_config(label: str) -> tuple[str, Any, dict] | None:
    """按 label 查询已注册的客户. 返回 ``(engine, config, kwargs)`` 或 ``None``."""
    return _REGISTRY.get(label)


def all_labels(*, engine: str | None = None) -> list[str]:
    """返回已注册客户 label 列表. 可按 engine 过滤 (simple/wide/complex)."""
    if engine is None:
        return list(_REGISTRY)
    return [label for label, (eng, _cfg, _kwargs) in _REGISTRY.items() if eng == engine]

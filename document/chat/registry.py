"""ChatHandler 注册表. 业务模块 import 时把自己的 handler 注册进来, ops 层按 name 取."""
from .handler import ChatHandler


HANDLERS: dict[str, ChatHandler] = {}


def register(handler: ChatHandler) -> None:
    HANDLERS[handler.name] = handler


def get(name: str) -> ChatHandler:
    h = HANDLERS.get(name)
    if h is None:
        raise KeyError(f"chat handler not registered: {name!r}")
    return h

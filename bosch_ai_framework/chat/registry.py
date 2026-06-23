"""Chat handler registry — register and look up business chat handlers."""

from bosch_ai_framework.chat.handler import ChatHandler

HANDLERS: dict[str, ChatHandler] = {}


def register(handler: ChatHandler) -> None:
    """Register a chat handler. Call at module import time."""
    HANDLERS[handler.name] = handler


def get(name: str) -> ChatHandler:
    """Look up a chat handler by name. Raises KeyError if not found."""
    h = HANDLERS.get(name)
    if h is None:
        raise KeyError(f"Chat handler not registered: {name!r}")
    return h

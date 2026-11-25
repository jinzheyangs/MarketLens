"""事件消费者处理器装配。"""

from __future__ import annotations

from app.core.config import Settings
from app.events.consumer import EventHandlerRegistry


def build_event_handlers(settings: Settings) -> EventHandlerRegistry:
    del settings
    return EventHandlerRegistry()


__all__ = ["build_event_handlers"]

"""Event-consumer handler assembly.

Stage 8 extends this registry with monitoring and notification handlers.
"""

from __future__ import annotations

from app.core.config import Settings
from app.events.consumer import EventHandlerRegistry


def build_event_handlers(settings: Settings) -> EventHandlerRegistry:
    del settings
    return EventHandlerRegistry()


__all__ = ["build_event_handlers"]

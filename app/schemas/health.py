"""Health probe response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ComponentHealth(BaseModel):
    status: Literal["ok", "error", "disabled"]
    latency_ms: float | None = Field(default=None, ge=0)


class LivenessResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    version: str
    components: dict[str, ComponentHealth]

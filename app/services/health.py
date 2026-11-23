"""Dependency readiness checks.

Failures are reported only as component status; exception details, DSNs, and
upstream response bodies stay in server logs (and are redacted there).
"""

from __future__ import annotations

import asyncio
import time

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.logging import get_logger
from app.schemas.health import ComponentHealth, LivenessResponse, ReadinessResponse

logger = get_logger(__name__)


class HealthService:
    def __init__(self, *, engine: AsyncEngine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings

    async def liveness(self) -> LivenessResponse:
        return LivenessResponse(version=self._settings.version)

    async def readiness(self) -> ReadinessResponse:
        components: dict[str, ComponentHealth] = {}

        checks: list[tuple[str, asyncio.Task[ComponentHealth]]] = [
            ("database", asyncio.create_task(self._check_database()))
        ]
        if self._settings.qdrant_url:
            checks.append(("vector_store", asyncio.create_task(self._check_qdrant())))
        else:
            components["vector_store"] = ComponentHealth(status="disabled")

        results = await asyncio.gather(*(task for _, task in checks), return_exceptions=True)
        for (name, _), result in zip(checks, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "health_component_exception",
                    extra={"context": {"component": name, "error": type(result).__name__}},
                )
                components[name] = ComponentHealth(status="error")
            else:
                components[name] = result

        healthy = all(component.status in {"ok", "disabled"} for component in components.values())
        return ReadinessResponse(
            status="ok" if healthy else "unavailable",
            version=self._settings.version,
            components=components,
        )

    async def _check_database(self) -> ComponentHealth:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._settings.health_check_timeout_seconds):
                async with self._engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning(
                "database_readiness_failed",
                extra={"context": {"component": "database", "error": type(exc).__name__}},
            )
            return ComponentHealth(
                status="error",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        return ComponentHealth(
            status="ok",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def _check_qdrant(self) -> ComponentHealth:
        started = time.perf_counter()
        url = self._settings.qdrant_url
        if url is None:  # pragma: no cover - guarded by caller
            return ComponentHealth(status="disabled")
        headers: dict[str, str] = {}
        if self._settings.qdrant_api_key is not None:
            headers["api-key"] = self._settings.qdrant_api_key.get_secret_value()
        try:
            async with asyncio.timeout(self._settings.health_check_timeout_seconds):
                # Operator-configured internal URL; no redirects or environment
                # proxies are followed.
                async with httpx.AsyncClient(
                    timeout=self._settings.qdrant_timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    response = await client.get(f"{url}/readyz", headers=headers)
                    response.raise_for_status()
        except Exception as exc:
            logger.warning(
                "vector_store_readiness_failed",
                extra={"context": {"component": "vector_store", "error": type(exc).__name__}},
            )
            return ComponentHealth(
                status="error",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        return ComponentHealth(
            status="ok",
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

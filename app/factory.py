"""Application factory.

Kept separate from ``app.main`` so tests can construct apps with explicit
settings without triggering module-level configuration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.error_handlers import register_exception_handlers
from app.api.health import router as health_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    AccessLogMiddleware,
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    RequestTimeoutMiddleware,
    SecurityHeadersMiddleware,
    TrustedHostMiddleware,
    UnhandledErrorMiddleware,
)
from app.core.telemetry import TelemetryHandle, instrument_database, setup_telemetry
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.services.health import HealthService

logger = get_logger(__name__)


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        try:
            app.state.engine = engine
            app.state.session_factory = create_session_factory(engine)
            app.state.health_service = HealthService(engine=engine, settings=settings)

            telemetry_handle: TelemetryHandle | None = getattr(app.state, "telemetry", None)
            if telemetry_handle is not None:
                instrument_database(engine, telemetry_handle)

            logger.info(
                "service_started",
                extra={
                    "context": {
                        "environment": settings.environment,
                        "database": settings.safe_database_url,
                        "tracing": settings.otel_enabled,
                    }
                },
            )
            yield
        finally:
            telemetry_handle = getattr(app.state, "telemetry", None)
            if telemetry_handle is not None:
                telemetry_handle.shutdown()
            await engine.dispose()
            logger.info("service_stopped")

    return lifespan


def _configure_middleware(app: FastAPI, settings: Settings) -> None:
    # Added innermost first; Starlette executes the last-added middleware first.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.effective_allowed_hosts,
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
    )
    app.add_middleware(
        RequestTimeoutMiddleware,
        timeout_seconds=settings.request_timeout_seconds,
    )
    app.add_middleware(UnhandledErrorMiddleware)

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=settings.cors_allow_credentials,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", settings.request_id_header],
            expose_headers=[settings.request_id_header],
            max_age=600,
        )

    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.environment == "production",
    )
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(
        RequestContextMiddleware,
        header_name=settings.request_id_header,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    configure_logging(configured)
    install_tenant_guard()

    docs_url = "/docs" if configured.api_docs_enabled else None
    openapi_url = "/openapi.json" if configured.api_docs_enabled else None
    redoc_url = "/redoc" if configured.api_docs_enabled else None

    app = FastAPI(
        title=configured.app_name,
        version=configured.version,
        docs_url=docs_url,
        openapi_url=openapi_url,
        redoc_url=redoc_url,
        lifespan=_build_lifespan(configured),
    )
    app.state.settings = configured

    register_exception_handlers(app)
    app.include_router(health_router)
    _configure_middleware(app, configured)

    if configured.otel_enabled:
        app.state.telemetry = setup_telemetry(app, configured)

    return app

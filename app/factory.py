"""Application factory.

Kept separate from ``app.main`` so tests can construct apps with explicit
settings without triggering module-level configuration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.service import AgentServices, build_agent_services
from app.api.error_handlers import register_exception_handlers
from app.api.health import router as health_router
from app.api.metrics import build_metrics_router
from app.api.v1.router import router as api_v1_router
from app.auth.middleware import AuthenticationMiddleware
from app.auth.oidc import build_token_verifier
from app.collector.service import CollectorServices, build_collector_services
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.core.metrics import Metrics, MetricsMiddleware
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
from app.db.rls import install_rls_context
from app.db.session import create_engine, create_session_factory
from app.db.tenant_guard import install_tenant_guard
from app.monitoring.service import MonitoringServices, build_monitoring_services
from app.reporting.service import ReviewServices, build_review_services
from app.services.health import HealthService
from app.services.rag import RagServices, build_rag_services

logger = get_logger(__name__)


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        rag_services: RagServices | None = None
        collector_services: CollectorServices | None = None
        agent_services: AgentServices | None = None
        review_services: ReviewServices | None = None
        monitoring_services: MonitoringServices | None = None
        try:
            app.state.engine = engine
            session_factory = create_session_factory(engine)
            app.state.session_factory = session_factory

            if settings.rag_enabled:
                rag_services = build_rag_services(settings, session_factory)
                await rag_services.ensure_ready()
            if settings.collector_enabled:
                collector_services = build_collector_services(
                    settings,
                    session_factory,
                )
                await collector_services.ensure_ready()
            if settings.agent_enabled:
                agent_services = build_agent_services(
                    settings,
                    session_factory,
                    rag_services=rag_services,
                    collector_services=collector_services,
                )
            if settings.report_enabled:
                review_services = build_review_services(settings, session_factory)
                await review_services.ensure_ready()
            if settings.monitoring_enabled:
                monitoring_services = build_monitoring_services(
                    settings,
                    session_factory,
                    collector_services=collector_services,
                )
            app.state.rag_services = rag_services
            app.state.collector_services = collector_services
            app.state.agent_services = agent_services
            app.state.review_services = review_services
            app.state.monitoring_services = monitoring_services
            app.state.health_service = HealthService(
                engine=engine,
                settings=settings,
                rag_services=rag_services,
                collector_services=collector_services,
            )

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
                        "rag": settings.rag_enabled,
                        "collector": settings.collector_enabled,
                        "agent": settings.agent_enabled,
                        "report": settings.report_enabled,
                        "monitoring": settings.monitoring_enabled,
                    }
                },
            )
            yield
        finally:
            if monitoring_services is not None:
                await monitoring_services.aclose()
            if review_services is not None:
                await review_services.aclose()
            if agent_services is not None:
                await agent_services.aclose()
            if collector_services is not None:
                await collector_services.aclose()
            if rag_services is not None:
                await rag_services.aclose()
            telemetry_handle = getattr(app.state, "telemetry", None)
            if telemetry_handle is not None:
                telemetry_handle.shutdown()
            await engine.dispose()
            logger.info("service_stopped")

    return lifespan


def _configure_middleware(
    app: FastAPI,
    settings: Settings,
    metrics: Metrics | None,
) -> None:
    # Added innermost first; Starlette executes the last-added middleware first.
    app.add_middleware(
        AuthenticationMiddleware,
        api_prefix=settings.api_prefix,
        verifier_provider=lambda: app.state.token_verifier,
    )
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
    if metrics is not None:
        app.add_middleware(
            MetricsMiddleware,
            metrics=metrics,
            ignored_paths=[settings.metrics_path],
        )
    app.add_middleware(
        RequestContextMiddleware,
        header_name=settings.request_id_header,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or get_settings()
    configure_logging(configured)
    install_tenant_guard()
    install_rls_context()

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
    app.state.token_verifier = build_token_verifier(configured)
    metrics = Metrics(configured) if configured.metrics_enabled else None
    app.state.metrics = metrics

    register_exception_handlers(app)
    app.include_router(health_router)
    if metrics is not None:
        app.include_router(build_metrics_router(configured))
    app.include_router(api_v1_router, prefix=configured.api_prefix)
    _configure_middleware(app, configured, metrics)

    if configured.otel_enabled:
        app.state.telemetry = setup_telemetry(app, configured)

    return app

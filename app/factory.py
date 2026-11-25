"""应用工厂（Application Factory）。

把 ``FastAPI`` 实例的构建逻辑从 ``app.main`` 中拆分出来，
便于测试时显式传入配置构建应用，而不会在模块导入阶段就触发全局配置加载。

职责：
- 构建 FastAPI 实例、注册路由与异常处理；
- 装配中间件链（鉴权、安全头、超时、日志、指标等）；
- 在生命周期内初始化/释放各领域服务（RAG、采集、Agent、报告、监测等）。
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
    """构建 FastAPI 生命周期上下文管理器。

    启动阶段：创建数据库引擎与会话工厂，按配置开关依次初始化各领域服务，
    并挂到 ``app.state`` 上供请求依赖取用；关闭阶段逆序释放资源并销毁引擎。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 数据库引擎与会话工厂：所有领域服务共享同一连接池
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

            # RAG 知识入库与检索服务（可选）
            if settings.rag_enabled:
                rag_services = build_rag_services(settings, session_factory)
                await rag_services.ensure_ready()
            # 外部采集服务（可选）
            if settings.collector_enabled:
                collector_services = build_collector_services(
                    settings,
                    session_factory,
                )
                await collector_services.ensure_ready()
            # 多 Agent 调研服务（依赖 RAG 与采集能力）
            if settings.agent_enabled:
                agent_services = build_agent_services(
                    settings,
                    session_factory,
                    rag_services=rag_services,
                    collector_services=collector_services,
                )
            # 证据审查与报告服务（可选）
            if settings.report_enabled:
                review_services = build_review_services(settings, session_factory)
                await review_services.ensure_ready()
            # 竞品/关键词监测服务（可选）
            if settings.monitoring_enabled:
                monitoring_services = build_monitoring_services(
                    settings,
                    session_factory,
                    collector_services=collector_services,
                )

            # 领域服务统一挂到 app.state，供 API 依赖注入取用
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

            # 若启用了 OpenTelemetry，对数据库引擎做自动埋点
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
            # 应用正式对外提供服务
            yield
        finally:
            # 关闭阶段逆序释放各领域服务资源
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
            # 关闭遥测并销毁数据库连接池
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
    """按顺序装配 HTTP 中间件链。

    注意：Starlette 中后添加的中间件先执行，因此这里先添加的是更靠近
    业务路由的内层中间件。
    """
    # 鉴权中间件：校验 Bearer 令牌并绑定租户上下文（最靠近业务）
    app.add_middleware(
        AuthenticationMiddleware,
        api_prefix=settings.api_prefix,
        verifier_provider=lambda: app.state.token_verifier,
    )
    # 受信任主机名校验
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.effective_allowed_hosts,
    )
    # 请求体大小限制
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_request_body_bytes,
    )
    # 请求超时控制
    app.add_middleware(
        RequestTimeoutMiddleware,
        timeout_seconds=settings.request_timeout_seconds,
    )
    # 未捕获异常兜底处理
    app.add_middleware(UnhandledErrorMiddleware)

    # 跨域配置（仅在显式配置允许来源时启用）
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

    # 安全响应头（生产环境开启 HSTS）
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.environment == "production",
    )
    # 访问日志中间件
    app.add_middleware(AccessLogMiddleware)
    # Prometheus 指标采集中间件（忽略指标自身路径）
    if metrics is not None:
        app.add_middleware(
            MetricsMiddleware,
            metrics=metrics,
            ignored_paths=[settings.metrics_path],
        )
    # 请求上下文（请求 ID 透传等）
    app.add_middleware(
        RequestContextMiddleware,
        header_name=settings.request_id_header,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用实例。

    :param settings: 显式传入的配置；为空时从环境变量加载默认配置。
    :returns: 装配好路由、中间件、异常处理与生命周期的 FastAPI 实例。
    """
    configured = settings or get_settings()     #从环境变量读取并存入setting
    configure_logging(configured)
    # 安装租户守卫与行级安全上下文（ORM 层强制租户范围）
    install_tenant_guard()
    install_rls_context()

    # 接口文档按配置开关决定是否暴露
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
    # 令牌校验器（OIDC/JWT 非对称校验）
    app.state.token_verifier = build_token_verifier(configured)
    metrics = Metrics(configured) if configured.metrics_enabled else None
    app.state.metrics = metrics

    # 注册全局异常处理与路由
    register_exception_handlers(app)
    app.include_router(health_router)
    if metrics is not None:
        app.include_router(build_metrics_router(configured))
    app.include_router(api_v1_router, prefix=configured.api_prefix)
    _configure_middleware(app, configured, metrics)     #把中间件套上去

    # 可选：初始化 OpenTelemetry 遥测
    if configured.otel_enabled:
        app.state.telemetry = setup_telemetry(app, configured)

    return app

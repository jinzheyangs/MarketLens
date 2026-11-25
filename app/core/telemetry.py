"""可选的 OpenTelemetry 链路追踪。

追踪默认关闭。启用后 Span 通过 OTLP/HTTP 导出，并借助 trace/span ID 与
日志关联。请求/响应体绝不记录；请求头采集保留埋点默认值（敏感头不采集）。
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings

# 健康检查路径不纳入追踪，避免噪音
_HEALTH_URLS = "health/live,health/ready"


@dataclass(slots=True)
class TelemetryHandle:
    """持有追踪与指标 Provider，便于在应用关闭时统一关停。"""

    provider: TracerProvider
    meter_provider: MeterProvider

    def shutdown(self) -> None:
        """关停指标与追踪 Provider， flush 剩余导出数据。"""
        self.meter_provider.shutdown()
        self.provider.shutdown()


def _signal_endpoint(endpoint: str, signal: str) -> str:
    """由 traces 端点推导 OTLP/HTTP 各信号（metrics/logs）的端点地址。"""
    parts = urlsplit(endpoint)
    path = parts.path
    suffix = f"/v1/{signal}"
    if path.endswith("/v1/traces"):
        path = path[: -len("/v1/traces")] + suffix
    elif not path.endswith(suffix):
        path = path.rstrip("/") + suffix
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _metrics_endpoint(traces_endpoint: str) -> str:
    """推导 metrics 信号端点。"""
    return _signal_endpoint(traces_endpoint, "metrics")


def _logs_endpoint(traces_endpoint: str) -> str:
    """推导 logs 信号端点。"""
    return _signal_endpoint(traces_endpoint, "logs")


def setup_telemetry(app: FastAPI, settings: Settings) -> TelemetryHandle:
    """创建 Provider 并对 FastAPI / HTTPX 自动埋点。

    必须在应用构建阶段、ASGI 应用启动前调用。
    """
    if not settings.otel_enabled or settings.otel_exporter_otlp_endpoint is None:
        raise RuntimeError("Telemetry requested without OTEL_EXPORTER_OTLP_ENDPOINT")

    # 资源标识：服务名、版本、部署环境
    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.version,
            "deployment.environment.name": settings.environment,
        }
    )
    # 采样：父链路已采样则跟随，否则按比例抽样
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_ratio)),
    )
    exporter = OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint,
        timeout=settings.otel_export_timeout_seconds,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # 指标 Provider 与周期导出 Reader
    metric_exporter = OTLPMetricExporter(
        endpoint=_metrics_endpoint(settings.otel_exporter_otlp_endpoint),
        timeout=settings.otel_export_timeout_seconds,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
    )
    metrics.set_meter_provider(meter_provider)

    # 自动埋点 FastAPI 与出站 HTTPX 客户端
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=_HEALTH_URLS,
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return TelemetryHandle(provider=provider, meter_provider=meter_provider)


def instrument_database(engine: AsyncEngine, handle: TelemetryHandle) -> None:
    """对指定异步引擎做 SQLAlchemy 埋点，在引擎创建后调用。"""
    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine,
        tracer_provider=handle.provider,
    )

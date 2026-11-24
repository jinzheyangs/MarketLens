"""Optional OpenTelemetry tracing.

Tracing is disabled by default. When enabled, spans are exported over OTLP/HTTP
and correlated with logs via trace/span IDs. Header capture is left at the
instrumentation default (disabled for sensitive headers); request/response
bodies are never recorded.
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

_HEALTH_URLS = "health/live,health/ready"


@dataclass(slots=True)
class TelemetryHandle:
    provider: TracerProvider
    meter_provider: MeterProvider

    def shutdown(self) -> None:
        self.meter_provider.shutdown()
        self.provider.shutdown()


def _signal_endpoint(endpoint: str, signal: str) -> str:
    """Derive an OTLP/HTTP signal endpoint from the traces endpoint."""
    parts = urlsplit(endpoint)
    path = parts.path
    suffix = f"/v1/{signal}"
    if path.endswith("/v1/traces"):
        path = path[: -len("/v1/traces")] + suffix
    elif not path.endswith(suffix):
        path = path.rstrip("/") + suffix
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _metrics_endpoint(traces_endpoint: str) -> str:
    return _signal_endpoint(traces_endpoint, "metrics")


def _logs_endpoint(traces_endpoint: str) -> str:
    return _signal_endpoint(traces_endpoint, "logs")


def setup_telemetry(app: FastAPI, settings: Settings) -> TelemetryHandle:
    """Create the provider and instrument FastAPI/HTTPX.

    Must be called during app construction, before the ASGI application starts.
    """
    if not settings.otel_enabled or settings.otel_exporter_otlp_endpoint is None:
        raise RuntimeError("Telemetry requested without OTEL_EXPORTER_OTLP_ENDPOINT")

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": settings.version,
            "deployment.environment.name": settings.environment,
        }
    )
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

    metric_exporter = OTLPMetricExporter(
        endpoint=_metrics_endpoint(settings.otel_exporter_otlp_endpoint),
        timeout=settings.otel_export_timeout_seconds,
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(metric_exporter)],
    )
    metrics.set_meter_provider(meter_provider)

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=_HEALTH_URLS,
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return TelemetryHandle(provider=provider, meter_provider=meter_provider)


def instrument_database(engine: AsyncEngine, handle: TelemetryHandle) -> None:
    """Instrument a specific async engine; called after engine creation."""
    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine,
        tracer_provider=handle.provider,
    )

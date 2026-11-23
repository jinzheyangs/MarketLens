"""Optional OpenTelemetry tracing.

Tracing is disabled by default. When enabled, spans are exported over OTLP/HTTP
and correlated with logs via trace/span IDs. Header capture is left at the
instrumentation default (disabled for sensitive headers); request/response
bodies are never recorded.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
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

    def shutdown(self) -> None:
        self.provider.shutdown()


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

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=_HEALTH_URLS,
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return TelemetryHandle(provider=provider)


def instrument_database(engine: AsyncEngine, handle: TelemetryHandle) -> None:
    """Instrument a specific async engine; called after engine creation."""
    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine,
        tracer_provider=handle.provider,
    )

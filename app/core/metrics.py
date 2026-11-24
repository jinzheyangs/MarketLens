"""Prometheus-compatible operational metrics.

Metrics are deliberately low-cardinality: HTTP labels use the templated route
path (not raw URLs) and never include tenant, user, host, or request IDs.
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import Counter as OtelCounter
from opentelemetry.metrics import Histogram as OtelHistogram
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class Metrics:
    def __init__(self, settings: Settings) -> None:
        self._otel_enabled = settings.otel_enabled
        self._otel_service_name = settings.otel_service_name
        self._otel_initialized = False
        self._otel_requests: OtelCounter | None = None
        self._otel_duration: OtelHistogram | None = None
        self.registry = CollectorRegistry(auto_describe=True)
        self.http_requests = Counter(
            "marketlens_http_requests_total",
            "HTTP requests processed by the API.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        self.http_request_duration = Histogram(
            "marketlens_http_request_duration_seconds",
            "HTTP request latency in seconds.",
            ("method", "route"),
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
            registry=self.registry,
        )
        self.http_requests_in_progress = Gauge(
            "marketlens_http_requests_in_progress",
            "HTTP requests currently being processed.",
            registry=self.registry,
        )
        self.http_server_errors = Counter(
            "marketlens_http_server_errors_total",
            "HTTP responses with status >= 500.",
            ("method", "route"),
            registry=self.registry,
        )
        self.app_info = Gauge(
            "marketlens_app_info",
            "Static build/deployment information.",
            ("service", "version", "environment"),
            registry=self.registry,
        )
        self.app_info.labels(
            service=settings.otel_service_name,
            version=settings.version,
            environment=settings.environment,
        ).set(1)

    def render(self) -> bytes:
        return generate_latest(self.registry)

    def _ensure_otel(self) -> None:
        if self._otel_initialized or not self._otel_enabled:
            return
        self._otel_initialized = True
        meter = otel_metrics.get_meter(self._otel_service_name)
        self._otel_requests = meter.create_counter(
            "marketlens.http.requests",
            unit="1",
            description="HTTP requests processed by the API.",
        )
        self._otel_duration = meter.create_histogram(
            "marketlens.http.request.duration",
            unit="s",
            description="HTTP request latency.",
        )

    def record(
        self,
        *,
        method: str,
        route: str,
        status: int,
        duration: float,
    ) -> None:
        self._ensure_otel()
        status_label = str(status)
        self.http_requests.labels(
            method=method,
            route=route,
            status=status_label,
        ).inc()
        self.http_request_duration.labels(method=method, route=route).observe(duration)
        if status >= 500:
            self.http_server_errors.labels(method=method, route=route).inc()
        if self._otel_requests is not None:
            attributes = {"http.request.method": method, "http.route": route}
            self._otel_requests.add(1, {**attributes, "http.response.status_code": status})
        if self._otel_duration is not None:
            self._otel_duration.record(
                duration,
                {"http.request.method": method, "http.route": route},
            )

    @property
    def content_type(self) -> str:
        return CONTENT_TYPE_LATEST


class MetricsMiddleware:
    """Record request count, latency, in-flight gauge, and 5xx count."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: Metrics,
        ignored_paths: Iterable[str] = (),
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.ignored_paths = frozenset(ignored_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        if method not in _HTTP_METHODS:
            method = "OTHER"
        started = time.perf_counter()
        status = 500
        self.metrics.http_requests_in_progress.inc()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.metrics.http_requests_in_progress.dec()
            route = scope.get("route")
            route_path = str(getattr(route, "path", ""))
            if not route_path:
                route_path = "unmatched"
            if route_path not in self.ignored_paths:
                self.metrics.record(
                    method=method,
                    route=route_path,
                    status=status,
                    duration=time.perf_counter() - started,
                )


__all__ = ["Metrics", "MetricsMiddleware"]

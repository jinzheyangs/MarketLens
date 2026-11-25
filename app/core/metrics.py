"""Prometheus 兼容的运行指标采集。

指标刻意保持低基数（low-cardinality）：HTTP 标签使用模板化路由路径
（而非原始 URL），绝不包含租户、用户、主机或请求 ID。
"""

from __future__ import annotations

import time
from collections.abc import Iterable

from opentelemetry import metrics as otel_metrics
from opentelemetry.metrics import Counter as OtelCounter
from opentelemetry.metrics import Histogram as OtelHistogram
from prometheus_client import (
    CONTENT_TYPE_LATEST,    #Prometheus 指标接口标准响应头。
    CollectorRegistry,      #指标注册表，所有指标对象注册在这里；
    Counter,        #计数器，只能增加，不能减少；
    Gauge,      #仪表盘，可以增可以减；
    Histogram,      #直方图，统计耗时，自动分桶，计算 P50/P95/P99；
    generate_latest,        #把注册表里面全部指标转成 Prometheus 协议文本；
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import Settings

_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}    #集合，标准 HTTP 方法，用来判断请求 method。非标准方法统一归类 OTHER。


class Metrics:
    """持有 Prometheus 注册表与各类指标，并按需同步到 OTel 仪表。"""

    def __init__(self, settings: Settings) -> None:
        self._otel_enabled = settings.otel_enabled  #配置开关，是否启用 OpenTelemetry 指标；
        self._otel_service_name = settings.otel_service_name    #OTel 服务名称；
        self._otel_initialized = False      #标记 OTel 指标是否已经初始化，避免重复创建；
        self._otel_requests: OtelCounter | None = None
        self._otel_duration: OtelHistogram | None = None        #OTel 版本计数器、直方图，没开启就是 None。

        self.registry = CollectorRegistry(auto_describe=True)   #创建 Prometheus 注册表，所有指标全部注册到这个 registry，不和全局默认注册表混用。
        # 已处理 HTTP 请求计数（按方法/路由/状态码）
        self.http_requests = Counter(
            "marketscope_http_requests_total",
            "HTTP requests processed by the API.",
            ("method", "route", "status"),
            registry=self.registry,
        )
        # HTTP 请求耗时直方图（秒）
        self.http_request_duration = Histogram(
            "marketscope_http_request_duration_seconds",
            "HTTP request latency in seconds.",
            ("method", "route"),
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
            registry=self.registry,
        )
        # 正在处理中的请求数（Gauge）
        self.http_requests_in_progress = Gauge(
            "marketscope_http_requests_in_progress",
            "HTTP requests currently being processed.",
            registry=self.registry,
        )
        # 5xx 响应计数
        self.http_server_errors = Counter(
            "marketscope_http_server_errors_total",
            "HTTP responses with status >= 500.",
            ("method", "route"),
            registry=self.registry,
        )
        # 静态构建/部署信息（常量 Gauge）
        self.app_info = Gauge(
            "marketscope_app_info",
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
        """把注册表当前指标序列化为 Prometheus 文本格式。"""
        return generate_latest(self.registry)

    def _ensure_otel(self) -> None:
        """懒初始化 OTel 仪表（仅在启用遥测时创建一次）。"""
        if self._otel_initialized or not self._otel_enabled:
            return
        self._otel_initialized = True
        meter = otel_metrics.get_meter(self._otel_service_name)
        self._otel_requests = meter.create_counter(
            "marketscope.http.requests",
            unit="1",
            description="HTTP requests processed by the API.",
        )
        self._otel_duration = meter.create_histogram(
            "marketscope.http.request.duration",
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
        """记录一次请求的计数、耗时与（必要时）5xx 计数，并同步 OTel。"""
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
        """Prometheus 文本指标的 Content-Type。"""
        return CONTENT_TYPE_LATEST


class MetricsMiddleware:
    """ASGI 中间件：记录请求计数、耗时、在飞请求数与 5xx 计数。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: Metrics,   #注入上边实例
        ignored_paths: Iterable[str] = (),  #需要忽略统计的路径，例如健康检查 `/health`；转成 frozenset，查询速度更快。
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.ignored_paths = frozenset(ignored_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 非 HTTP 事件（如 WebSocket 生命周期）直接透传
        if scope["type"] != "http":     #scope ["type"] 可以是 http、websocket。非 http 直接透传，不做指标统计。
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()    #取出 http 请求方法，全部大写。不是标准 6 种 http 方法，统一标记为 OTHER。
        if method not in _HTTP_METHODS:
            method = "OTHER"
        started = time.perf_counter()   #记录请求开始高精度时间点，后面算耗时。
        status = 500    
        self.metrics.http_requests_in_progress.inc()    #请求进来，正在处理请求数 Gauge +1。

        async def send_wrapper(message: Message) -> None:
            # 从响应起始帧提取真实状态码
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.metrics.http_requests_in_progress.dec()    #请求结束，在飞请求数‑1。**极其重要，如果忘记 dec ()，Gauge 只会涨不会降。**
            # 取模板化路由路径；未匹配路由归入 unmatched
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

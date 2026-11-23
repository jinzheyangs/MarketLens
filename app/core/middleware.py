"""ASGI middleware for correlation, limits, security headers, and access logs.

Ordering (outermost first) is configured in ``app.factory``:

    RequestContext -> AccessLog -> SecurityHeaders -> CORS -> UnhandledError
    -> Timeout -> BodySizeLimit -> TrustedHost -> application
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import (
    is_safe_request_id,
    reset_request_id,
    set_request_id,
)
from app.core.errors import ErrorCode, RequestEntityTooLargeError
from app.core.responses import error_response

logger = logging.getLogger(__name__)


class RequestContextMiddleware:
    """Assign a safe request ID and expose it in responses and logs."""

    def __init__(self, app: ASGIApp, *, header_name: str) -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        inbound = Headers(scope=scope).get(self.header_name)
        request_id = inbound if inbound and is_safe_request_id(inbound) else uuid4().hex

        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["request_id"] = request_id

        token = set_request_id(request_id)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[self.header_name] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_request_id(token)


class AccessLogMiddleware:
    """Emit one structured log line per request without query strings or headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = str(scope.get("path", ""))
            route = scope.get("route")
            route_path = str(getattr(route, "path", path))
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            client = scope.get("client")
            client_host = client[0] if isinstance(client, tuple) and client else None
            context: dict[str, Any] = {
                "method": scope.get("method"),
                "path": route_path,
                "status": status_code,
                "duration_ms": duration_ms,
            }
            state = scope.get("state")
            if isinstance(state, dict) and state.get("tenant_id") is not None:
                context["tenant_id"] = state["tenant_id"]
            if client_host is not None:
                context["client"] = client_host
            if path.startswith("/health/"):
                level = logging.DEBUG if status_code < 400 else logging.WARNING
            elif status_code >= 500:
                level = logging.ERROR
            elif status_code >= 400:
                level = logging.WARNING
            else:
                level = logging.INFO
            logger.log(level, "http_request", extra={"context": context})


class UnhandledErrorMiddleware:
    """Convert unexpected exceptions inside the wrapped stack into a 500 envelope.

    Placing this inside the correlation/security/CORS wrappers ensures even an
    unexpected failure still receives a request ID, security headers, and CORS
    headers. The Starlette outer handler remains a last-resort fallback.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            logger.error(
                "unhandled_exception",
                extra={
                    "context": {
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                    }
                },
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            if response_started:
                logger.error(
                    "unhandled_exception_after_response_started",
                    extra={"context": {"path": scope.get("path")}},
                )
                return
            response = error_response(
                500,
                ErrorCode.INTERNAL_ERROR,
                "An internal error occurred.",
            )
            await response(scope, receive, send)


def parse_host_header(host_header: str | None) -> str | None:
    """Extract a normalized host from an untrusted Host header.

    Returns ``None`` for malformed values. IPv6 literals keep their brackets;
    no DNS/IDNA resolution or network access is performed.
    """
    if host_header is None:
        return None
    value = host_header.strip()
    if not value or any(character in value for character in " \t\r\n/\\@"):
        return None

    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return None
        host = value[: closing + 1]
        remainder = value[closing + 1 :]
        if remainder and not (remainder.startswith(":") and remainder[1:].isdigit()):
            return None
        return host.lower()

    if value.count(":") > 1:
        return None
    host, separator, port = value.partition(":")
    if not host or (separator and not port.isdigit()):
        return None
    return host.lower()


class TrustedHostMiddleware:
    """Host allowlist with the unified JSON error envelope.

    Mirrors Starlette's validated wildcard semantics (``*.example.com``) but
    intentionally does not perform ``www`` redirects; API hosts must be exact.
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Sequence[str]) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        for pattern in allowed_hosts:
            if "*" in pattern[1:]:
                raise ValueError(f"Invalid host wildcard pattern: {pattern}")
            if pattern.startswith("*") and pattern != "*" and not pattern.startswith("*."):
                raise ValueError(f"Invalid host wildcard pattern: {pattern}")
        self.app = app
        self.allowed_hosts = [pattern.lower() for pattern in allowed_hosts]
        self.allow_any = "*" in self.allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        host = parse_host_header(Headers(scope=scope).get("host"))
        if host is not None:
            allowed = any(
                host == pattern or (pattern.startswith("*") and host.endswith(pattern[1:]))
                for pattern in self.allowed_hosts
            )
            if allowed:
                await self.app(scope, receive, send)
                return

        response = error_response(
            400,
            ErrorCode.BAD_REQUEST,
            "The host header is not permitted.",
        )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    """Conservative security headers suitable for a JSON API."""

    def __init__(self, app: ASGIApp, *, enable_hsts: bool) -> None:
        self.app = app
        self.enable_hsts = enable_hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                defaults = {
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Cross-Origin-Opener-Policy": "same-origin",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                    "Cache-Control": "no-store",
                }
                for key, value in defaults.items():
                    if key not in headers:
                        headers[key] = value
                if self.enable_hsts and "Strict-Transport-Security" not in headers:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RequestTimeoutMiddleware:
    """Bound total request handling time."""

    def __init__(self, app: ASGIApp, *, timeout_seconds: float) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.app(scope, receive, send_wrapper)
        except TimeoutError:
            if response_started:
                logger.error(
                    "request_timeout_after_response_started",
                    extra={"context": {"path": scope.get("path")}},
                )
                return
            response = error_response(
                504,
                ErrorCode.REQUEST_TIMEOUT,
                "The request exceeded the permitted processing time.",
                retryable=True,
            )
            await response(scope, receive, send)


class BodySizeLimitMiddleware:
    """Reject oversized bodies before or while they are read.

    ``Content-Length`` is checked up front. Chunked/streamed bodies are counted
    as they arrive; exceeding the limit raises an ``AppError`` that the normal
    exception handler converts into the unified 413 envelope.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared_length = Headers(scope=scope).get("content-length")
        if declared_length is not None:
            try:
                declared_bytes = int(declared_length)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > self.max_body_bytes:
                response = error_response(
                    413,
                    ErrorCode.PAYLOAD_TOO_LARGE,
                    "The request body exceeds the permitted size.",
                )
                await response(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise RequestEntityTooLargeError
            return message

        await self.app(scope, limited_receive, send)

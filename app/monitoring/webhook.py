"""SSRF-safe webhook delivery with pinned DNS and no redirects."""

from __future__ import annotations

import asyncio
import http.client
import ssl
import time
from dataclasses import dataclass

from app.collector.errors import BlockedTargetError, InvalidTargetError
from app.collector.network import (
    Resolver,
    SafeUrl,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
    resolve_addresses,
    validate_url,
)
from app.core.config import Settings
from app.monitoring.errors import NotificationSendError, NotificationTargetError


@dataclass(frozen=True, slots=True)
class WebhookResult:
    status_code: int


class SafeWebhookClient:
    def __init__(
        self,
        settings: Settings,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._ssl_context = ssl.create_default_context()

    async def post_json(
        self,
        *,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> WebhookResult:
        if len(body) > self._settings.notification_max_payload_bytes:
            raise NotificationSendError
        try:
            async with asyncio.timeout(self._settings.notification_timeout_seconds + 1):
                return await asyncio.to_thread(self._post_sync, url, body, headers)
        except TimeoutError as exc:
            raise NotificationSendError from exc

    def validate_target(self, url: str) -> SafeUrl:
        return self._validate_target(url)

    def _validate_target(self, url: str) -> SafeUrl:
        try:
            safe = validate_url(url, self._settings)
        except (InvalidTargetError, BlockedTargetError) as exc:
            raise NotificationTargetError from exc
        if safe.scheme != "https" and not self._settings.notification_allow_http:
            raise NotificationTargetError
        allowed = self._settings.notification_allowed_domains
        if allowed:
            host = safe.host.lower()
            if not any(host == domain or host.endswith(f".{domain}") for domain in allowed):
                raise NotificationTargetError
        return safe

    def _post_sync(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
    ) -> WebhookResult:
        safe = self._validate_target(url)
        try:
            addresses = resolve_addresses(
                safe,
                self._settings,
                resolver=self._resolver,
            )
        except (BlockedTargetError, InvalidTargetError) as exc:
            raise NotificationTargetError from exc
        except Exception as exc:
            raise NotificationSendError from exc

        deadline = time.monotonic() + self._settings.notification_timeout_seconds
        last_error: Exception | None = None
        response: http.client.HTTPResponse | None = None
        connection: http.client.HTTPConnection | None = None
        for address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NotificationSendError
            try:
                connection = self._connect(safe, address, remaining)
                connection.connect()
                if connection.sock is not None:
                    connection.sock.settimeout(max(0.1, remaining))
                request_headers = {
                    "User-Agent": self._settings.collector_user_agent,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Content-Length": str(len(body)),
                    "Connection": "close",
                    **headers,
                }
                connection.request(
                    "POST",
                    safe.request_target,
                    body=body,
                    headers=request_headers,
                )
                response = connection.getresponse()
                break
            except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                last_error = exc
                if connection is not None:
                    connection.close()
                connection = None

        if response is None:
            raise NotificationSendError from last_error
        try:
            # Do not follow redirects for tenant-supplied webhook endpoints.
            if not 200 <= response.status < 300:
                raise NotificationSendError
            response.read(self._settings.notification_max_response_bytes + 1)
            return WebhookResult(status_code=response.status)
        finally:
            response.close()
            if connection is not None:
                connection.close()

    def _connect(
        self,
        safe: SafeUrl,
        address: str,
        timeout: float,
    ) -> http.client.HTTPConnection:
        if safe.scheme == "https":
            return _PinnedHTTPSConnection(
                safe.host,
                safe.port,
                address,
                timeout,
                self._ssl_context,
            )
        return _PinnedHTTPConnection(safe.host, safe.port, address, timeout)


__all__ = ["SafeWebhookClient", "WebhookResult"]

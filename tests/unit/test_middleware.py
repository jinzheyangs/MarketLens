from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from httpx import AsyncClient

from app.factory import create_app
from tests.conftest import make_settings, open_client

router = APIRouter()


@router.get("/echo")
async def echo() -> dict[str, bool]:
    return {"ok": True}


@router.post("/echo-body")
async def echo_body(request: Request) -> dict[str, int]:
    body = await request.body()
    return {"size": len(body)}


@router.get("/slow")
async def slow() -> dict[str, bool]:
    await asyncio.sleep(0.5)
    return {"ok": True}


@router.post("/stream")
async def stream(request: Request) -> dict[str, int]:
    body = await request.body()
    return {"size": len(body)}


@router.get("/boom")
async def boom() -> None:
    raise RuntimeError("internal detail")


async def _client_for(**overrides: object) -> AsyncIterator[AsyncClient]:
    application = create_app(make_settings(**overrides))
    application.include_router(router)
    async with open_client(application) as client:
        yield client


async def test_request_id_is_generated_and_returned() -> None:
    async for client in _client_for():
        response = await client.get("/echo")
        request_id = response.headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{32}", request_id)


async def test_safe_inbound_request_id_is_preserved() -> None:
    async for client in _client_for():
        response = await client.get("/echo", headers={"X-Request-ID": "req-12345678"})
        assert response.headers["x-request-id"] == "req-12345678"


async def test_unsafe_inbound_request_id_is_replaced() -> None:
    async for client in _client_for():
        response = await client.get("/echo", headers={"X-Request-ID": "bad id with spaces"})
        assert response.headers["x-request-id"] != "bad id with spaces"
        assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_security_headers_are_applied() -> None:
    async for client in _client_for():
        response = await client.get("/echo")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert "strict-transport-security" not in response.headers


async def test_declared_oversized_body_is_rejected() -> None:
    async for client in _client_for(max_request_body_bytes=1024):
        response = await client.post(
            "/echo-body",
            content=b"x" * 2048,
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 413
        error = response.json()["error"]
        assert error["code"] == "payload_too_large"
        assert response.headers["x-content-type-options"] == "nosniff"


async def test_streamed_oversized_body_is_rejected() -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(8):
            yield b"x" * 256

    async for client in _client_for(max_request_body_bytes=1024):
        response = await client.post(
            "/stream",
            content=chunks(),
            headers={"content-type": "application/octet-stream"},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "payload_too_large"


async def test_request_timeout_returns_504() -> None:
    async for client in _client_for(request_timeout_seconds=0.05):
        response = await client.get("/slow")
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "request_timeout"
        assert response.json()["error"]["retryable"] is True


async def test_cors_is_closed_by_default() -> None:
    async for client in _client_for():
        response = await client.get("/echo", headers={"Origin": "https://frontend.example.com"})
        assert "access-control-allow-origin" not in response.headers


async def test_cors_allows_configured_origin_only() -> None:
    async for client in _client_for(cors_allowed_origins=["https://frontend.example.com"]):
        allowed = await client.get("/echo", headers={"Origin": "https://frontend.example.com"})
        denied = await client.get("/echo", headers={"Origin": "https://evil.example.com"})
        assert allowed.headers["access-control-allow-origin"] == "https://frontend.example.com"
        assert "access-control-allow-origin" not in denied.headers


async def test_untrusted_host_is_rejected_with_unified_error() -> None:
    async for client in _client_for(allowed_hosts=["testserver"]):
        response = await client.get("/echo", headers={"Host": "evil.example.com"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "bad_request"
        assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
        assert response.headers["x-content-type-options"] == "nosniff"


async def test_wildcard_host_pattern_is_supported() -> None:
    async for client in _client_for(allowed_hosts=["*.example.com"]):
        allowed = await client.get("/echo", headers={"Host": "api.example.com"})
        denied = await client.get("/echo", headers={"Host": "example.org"})
        assert allowed.status_code == 200
        assert denied.status_code == 400


async def test_cors_headers_are_present_on_unhandled_errors() -> None:
    async for client in _client_for(cors_allowed_origins=["https://frontend.example.com"]):
        response = await client.get("/boom", headers={"Origin": "https://frontend.example.com"})
        assert response.status_code == 500
        assert response.headers["access-control-allow-origin"] == "https://frontend.example.com"
        assert response.headers["x-request-id"]

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import APIRouter
from httpx import AsyncClient

from app.core.errors import AppError, ErrorCode
from app.factory import create_app
from tests.conftest import make_settings, open_client

router = APIRouter()


@router.get("/boom")
async def boom() -> None:
    raise RuntimeError("database password leaked here")


@router.get("/app-error")
async def app_error() -> None:
    raise AppError(
        code=ErrorCode.CONFLICT,
        message="The resource already exists.",
        status_code=409,
        details={"field": "slug"},
    )


@router.get("/validated")
async def validated(limit: int) -> dict[str, int]:
    return {"limit": limit}


@pytest.fixture
async def error_client() -> AsyncIterator[AsyncClient]:
    application = create_app(make_settings())
    application.include_router(router)
    async with open_client(application) as client:
        yield client


async def test_app_error_uses_unified_envelope(error_client: AsyncClient) -> None:
    response = await error_client.get("/app-error")
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "conflict"
    assert body["message"] == "The resource already exists."
    assert body["details"] == {"field": "slug"}
    assert body["retryable"] is False
    assert body["request_id"] == response.headers["x-request-id"]


async def test_validation_errors_do_not_echo_input(error_client: AsyncClient) -> None:
    response = await error_client.get("/validated", params={"limit": "sensitive-input"})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"]
    assert "sensitive-input" not in response.text
    assert all("field" in item and "message" in item for item in body["details"])


async def test_unhandled_exception_is_not_leaked(error_client: AsyncClient) -> None:
    response = await error_client.get("/boom")
    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "internal_error"
    assert body["message"] == "An internal error occurred."
    assert "password" not in response.text
    # Even last-resort failures keep correlation and security headers.
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


async def test_not_found_uses_unified_envelope(error_client: AsyncClient) -> None:
    response = await error_client.get("/missing")
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "not_found"
    assert body["request_id"] == response.headers["x-request-id"]

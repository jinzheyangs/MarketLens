from __future__ import annotations

from fastapi import FastAPI
from httpx import AsyncClient

from app.factory import create_app
from app.schemas.health import ComponentHealth, LivenessResponse, ReadinessResponse
from tests.conftest import make_production_settings, open_client


class FakeHealthService:
    def __init__(self, ready: bool) -> None:
        self._ready = ready

    async def liveness(self) -> LivenessResponse:
        return LivenessResponse(version="test-version")

    async def readiness(self) -> ReadinessResponse:
        return ReadinessResponse(
            status="ok" if self._ready else "unavailable",
            version="test-version",
            components={
                "database": ComponentHealth(status="ok" if self._ready else "error"),
                "vector_store": ComponentHealth(status="disabled"),
            },
        )


async def test_liveness(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers["cache-control"] == "no-store"


async def test_readiness_ok(app: FastAPI, client: AsyncClient) -> None:
    app.state.health_service = FakeHealthService(ready=True)
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["vector_store"]["status"] == "disabled"


async def test_readiness_unavailable(app: FastAPI, client: AsyncClient) -> None:
    app.state.health_service = FakeHealthService(ready=False)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["components"]["database"]["status"] == "error"
    assert "password" not in response.text


async def test_production_disables_docs_and_enables_hsts() -> None:
    application = create_app(make_production_settings(allowed_hosts=["testserver"]))
    async with open_client(application) as client:
        docs = await client.get("/docs")
        schema = await client.get("/openapi.json")
        live = await client.get("/health/live")
    assert docs.status_code == 404
    assert schema.status_code == 404
    assert live.status_code == 200
    assert "strict-transport-security" in live.headers


async def test_development_docs_are_available(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Market Intelligence Backend"

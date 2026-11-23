from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.auth.middleware import AuthenticationMiddleware
from app.auth.oidc import TokenClaims, TokenVerificationError, TokenVerifier
from app.core.context import current_tenant_id


class StaticVerifier:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id

    async def verify(self, token: str) -> TokenClaims:
        if token == "good-token":
            return TokenClaims(
                issuer="https://idp.test",
                subject="user-1",
                tenant_id=self.tenant_id,
            )
        raise TokenVerificationError("invalid_token")


def _build_app(
    verifier: TokenVerifier | None,
    *,
    api_prefix: str = "/api/v1",
) -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/whoami")
    async def whoami(request: Request) -> dict[str, str | None]:
        return {"tenant_id": str(current_tenant_id()), "path": request.url.path}

    @app.get("/health/live")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/public")
    async def public() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(
        AuthenticationMiddleware,
        api_prefix=api_prefix,
        verifier_provider=lambda: verifier,
    )
    return app


async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client


async def test_missing_token_is_rejected_for_api_paths() -> None:
    app = _build_app(StaticVerifier(uuid4()))
    async for client in _client(app):
        response = await client.get("/api/v1/whoami")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"].startswith("Bearer")


async def test_public_paths_do_not_require_a_token() -> None:
    app = _build_app(StaticVerifier(uuid4()))
    async for client in _client(app):
        health = await client.get("/health/live")
        public = await client.get("/public")
    assert health.status_code == 200
    assert public.status_code == 200


async def test_valid_token_binds_and_resets_tenant_context() -> None:
    tenant_id = uuid4()
    app = _build_app(StaticVerifier(tenant_id))
    async for client in _client(app):
        response = await client.get(
            "/api/v1/whoami", headers={"Authorization": "Bearer good-token"}
        )
    assert response.status_code == 200
    assert response.json()["tenant_id"] == str(tenant_id)
    assert current_tenant_id() is None


async def test_invalid_token_does_not_echo_token_details() -> None:
    app = _build_app(StaticVerifier(uuid4()))
    async for client in _client(app):
        response = await client.get("/api/v1/whoami", headers={"Authorization": "Bearer bad-token"})
    assert response.status_code == 401
    assert response.json()["error"]["message"] == (
        "The authentication token is invalid or expired."
    )
    assert "bad-token" not in response.text
    assert 'error="invalid_token"' in response.headers["www-authenticate"]


async def test_non_bearer_and_oversized_headers_are_rejected() -> None:
    app = _build_app(StaticVerifier(uuid4()))
    async for client in _client(app):
        basic = await client.get("/api/v1/whoami", headers={"Authorization": "Basic abc"})
        oversized = await client.get(
            "/api/v1/whoami",
            headers={"Authorization": f"Bearer {'a' * 9000}"},
        )
    assert basic.status_code == 401
    assert oversized.status_code == 401


async def test_unconfigured_authentication_fails_closed() -> None:
    app = _build_app(None)
    async for client in _client(app):
        response = await client.get("/api/v1/whoami")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Authentication is not configured."


async def test_prefix_boundary_is_not_triggered_by_similar_paths() -> None:
    app = _build_app(StaticVerifier(uuid4()))
    async for client in _client(app):
        response = await client.get("/api/v10/public")
    assert response.status_code == 404

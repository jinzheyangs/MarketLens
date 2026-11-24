from __future__ import annotations

import pytest

from app.auth.oidc import JWTVerifier
from app.factory import create_app
from tests.conftest import make_settings, open_client

pytestmark = pytest.mark.integration

_TOKEN = "metrics-token-at-least-24-characters"


async def test_metrics_endpoint_requires_configured_bearer_token(
    test_database_url: str,
) -> None:
    settings = make_settings(
        database_url=test_database_url,
        log_level="WARNING",
        metrics_enabled=True,
        metrics_bearer_token=_TOKEN,
    )
    application = create_app(settings)
    application.state.token_verifier = JWTVerifier(
        issuer="https://idp.test",
        audience="marketscope-test",
        algorithms=("RS256",),
        key_resolver=lambda _raw_token: None,
    )
    async with open_client(application) as client:
        unauthorized = await client.get("/metrics")
        assert unauthorized.status_code == 401
        assert unauthorized.content == b""

        await client.get("/health/live")
        response = await client.get(
            "/metrics",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        assert "marketscope_app_info" in body
        assert "marketscope_http_requests_total" in body
        assert "marketscope_http_request_duration_seconds" in body
        assert "/health/live" in body
        # Tenant/user identifiers and raw paths must never become label values.
        assert "tenant" not in body.lower()


async def test_metrics_endpoint_is_absent_when_disabled(
    test_database_url: str,
) -> None:
    settings = make_settings(
        database_url=test_database_url,
        log_level="WARNING",
    )
    application = create_app(settings)
    application.state.token_verifier = JWTVerifier(
        issuer="https://idp.test",
        audience="marketscope-test",
        algorithms=("RS256",),
        key_resolver=lambda _raw_token: None,
    )
    async with open_client(application) as client:
        response = await client.get("/metrics")
        assert response.status_code == 404

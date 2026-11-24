from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.oidc import JWTVerifier
from app.auth.principal import resolve_principal_for_user
from app.collector.errors import BlockedTargetError
from app.collector.service import enqueue_tasks
from app.core.context import tenant_context
from app.factory import create_app
from tests.conftest import make_settings, open_client
from tests.integration.test_api_stage2 import (
    AUDIENCE,
    ISSUER,
    Seed,
    _auth,
    _seed_database,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    jwt_private_key: rsa.RSAPrivateKey,
) -> Seed:
    return await _seed_database(session_factory, jwt_private_key)


@pytest.fixture
async def policy_client(
    seed: Seed,
    test_database_url: str,
    jwt_public_key: rsa.RSAPublicKey,
) -> Any:
    settings = make_settings(
        database_url=test_database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        log_level="WARNING",
    )
    application = create_app(settings)
    application.state.token_verifier = JWTVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        algorithms=("RS256",),
        key_resolver=lambda _raw_token: jwt_public_key,
        leeway_seconds=0,
    )
    async with open_client(application) as client:
        yield client, seed, application, settings


async def test_domain_policy_api_and_enforcement(policy_client: Any) -> None:
    client, seed, application, settings = policy_client
    admin = _auth(seed.tokens["tenant_admin_a"])

    initial = await client.get("/api/v1/domain-policy", headers=admin)
    assert initial.status_code == 200
    assert initial.json() == {"allowed_domains": [], "blocked_domains": []}

    forbidden = await client.patch(
        "/api/v1/domain-policy",
        headers=_auth(seed.tokens["workspace_admin_a"]),
        json={"allowed_domains": ["example.test"]},
    )
    assert forbidden.status_code == 403

    updated = await client.patch(
        "/api/v1/domain-policy",
        headers=admin,
        json={
            "allowed_domains": ["example.test"],
            "blocked_domains": ["blocked.example.test"],
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["allowed_domains"] == ["example.test"]
    assert updated.json()["blocked_domains"] == ["blocked.example.test"]

    invalid = await client.patch(
        "/api/v1/domain-policy",
        headers=admin,
        json={"allowed_domains": ["bad/domain"]},
    )
    assert invalid.status_code == 400

    with tenant_context(seed.tenant_a):
        async with application.state.session_factory() as session:
            principal = await resolve_principal_for_user(
                session,
                tenant_id=seed.tenant_a,
                user_id=seed.user_ids["workspace_editor_a"],
            )
            with pytest.raises(BlockedTargetError):
                await enqueue_tasks(
                    session,
                    principal=principal,
                    workspace_id=seed.workspace_a,
                    project_id=None,
                    urls=["https://blocked.example.test/page"],
                    settings=settings,
                )
            with pytest.raises(BlockedTargetError):
                await enqueue_tasks(
                    session,
                    principal=principal,
                    workspace_id=seed.workspace_a,
                    project_id=None,
                    urls=["https://other.test/page"],
                    settings=settings,
                )
            tasks = await enqueue_tasks(
                session,
                principal=principal,
                workspace_id=seed.workspace_a,
                project_id=None,
                urls=["https://example.test/allowed"],
                settings=settings,
            )
            assert len(tasks) == 1

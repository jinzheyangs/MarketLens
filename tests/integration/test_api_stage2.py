from __future__ import annotations

import time
from dataclasses import dataclass
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.auth.oidc import JWTVerifier
from app.core.context import tenant_context
from app.db.models import (
    Organization,
    OrganizationMembership,
    Project,
    ProjectMembership,
    Tenant,
    User,
    Workspace,
    WorkspaceMembership,
)
from app.factory import create_app
from tests.conftest import make_settings, open_client

pytestmark = pytest.mark.integration

ISSUER = "https://idp.marketscope.test"
AUDIENCE = "marketscope-api"


@dataclass
class Seed:
    tenant_a: UUID
    tenant_b: UUID
    org_a: UUID
    workspace_a: UUID
    project_a: UUID
    org_b: UUID
    workspace_b: UUID
    project_b: UUID
    user_ids: dict[str, UUID]
    subjects: dict[str, str]
    tokens: dict[str, str]
    session_factory: async_sessionmaker[AsyncSession]


@dataclass
class ApiHarness:
    client: AsyncClient
    seed: Seed
    app: FastAPI
    private_key: rsa.RSAPrivateKey


def _token(
    private_key: rsa.RSAPrivateKey,
    *,
    subject: str,
    tenant_id: UUID,
    extra: dict[str, object] | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": subject,
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + 300,
    }
    if extra:
        claims.update(extra)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_user(
    *,
    name: str,
    subject: str,
    tenant_id: UUID,
    tenant_role: str,
) -> User:
    return User(
        id=uuid4(),
        tenant_id=tenant_id,
        issuer=ISSUER,
        subject=subject,
        email=f"{subject}-{uuid4().hex[:8]}@example.com",
        display_name=name.replace("_", " ").title(),
        tenant_role=tenant_role,
    )


async def _seed_database(
    session_factory: async_sessionmaker[AsyncSession],
    private_key: rsa.RSAPrivateKey,
) -> Seed:
    tenant_a = uuid4()
    tenant_b = uuid4()
    org_a = uuid4()
    workspace_a = uuid4()
    project_a = uuid4()
    org_b = uuid4()
    workspace_b = uuid4()
    project_b = uuid4()

    async with session_factory() as session:
        session.add_all(
            [
                Tenant(id=tenant_a, slug=f"acme-{tenant_a.hex[:8]}", name="Acme"),
                Tenant(id=tenant_b, slug=f"beta-{tenant_b.hex[:8]}", name="Beta"),
            ]
        )
        await session.commit()

    user_specs = {
        "tenant_admin_a": ("tenant-admin-a", "tenant_admin", tenant_a),
        "org_admin_a": ("org-admin-a", "member", tenant_a),
        "org_editor_a": ("org-editor-a", "member", tenant_a),
        "org_viewer_a": ("org-viewer-a", "member", tenant_a),
        "workspace_admin_a": ("workspace-admin-a", "member", tenant_a),
        "workspace_editor_a": ("workspace-editor-a", "member", tenant_a),
        "workspace_viewer_a": ("workspace-viewer-a", "member", tenant_a),
        "project_editor_a": ("project-editor-a", "member", tenant_a),
        "outsider_a": ("outsider-a", "member", tenant_a),
        "tenant_admin_b": ("tenant-admin-b", "tenant_admin", tenant_b),
    }

    users: dict[str, User] = {}
    for name, (subject, tenant_role, tenant_id) in user_specs.items():
        users[name] = _make_user(
            name=name,
            subject=subject,
            tenant_id=tenant_id,
            tenant_role=tenant_role,
        )

    with tenant_context(tenant_a):
        async with session_factory() as session:
            session.add_all([user for user in users.values() if user.tenant_id == tenant_a])
            session.add(Organization(id=org_a, tenant_id=tenant_a, slug="org-a", name="Org A"))
            await session.flush()
            session.add(
                Workspace(
                    id=workspace_a,
                    tenant_id=tenant_a,
                    organization_id=org_a,
                    slug="workspace-a",
                    name="Workspace A",
                )
            )
            await session.flush()
            session.add(
                Project(
                    id=project_a,
                    tenant_id=tenant_a,
                    workspace_id=workspace_a,
                    slug="project-a",
                    name="Project A",
                )
            )
            await session.flush()
            session.add_all(
                [
                    OrganizationMembership(
                        tenant_id=tenant_a,
                        organization_id=org_a,
                        user_id=users["org_admin_a"].id,
                        role="admin",
                    ),
                    OrganizationMembership(
                        tenant_id=tenant_a,
                        organization_id=org_a,
                        user_id=users["org_editor_a"].id,
                        role="editor",
                    ),
                    OrganizationMembership(
                        tenant_id=tenant_a,
                        organization_id=org_a,
                        user_id=users["org_viewer_a"].id,
                        role="viewer",
                    ),
                    WorkspaceMembership(
                        tenant_id=tenant_a,
                        workspace_id=workspace_a,
                        user_id=users["workspace_admin_a"].id,
                        role="admin",
                    ),
                    WorkspaceMembership(
                        tenant_id=tenant_a,
                        workspace_id=workspace_a,
                        user_id=users["workspace_editor_a"].id,
                        role="editor",
                    ),
                    WorkspaceMembership(
                        tenant_id=tenant_a,
                        workspace_id=workspace_a,
                        user_id=users["workspace_viewer_a"].id,
                        role="viewer",
                    ),
                    ProjectMembership(
                        tenant_id=tenant_a,
                        project_id=project_a,
                        user_id=users["project_editor_a"].id,
                        role="editor",
                    ),
                ]
            )
            await session.commit()

    with tenant_context(tenant_b):
        async with session_factory() as session:
            session.add(users["tenant_admin_b"])
            session.add(Organization(id=org_b, tenant_id=tenant_b, slug="org-b", name="Org B"))
            await session.flush()
            session.add(
                Workspace(
                    id=workspace_b,
                    tenant_id=tenant_b,
                    organization_id=org_b,
                    slug="workspace-b",
                    name="Workspace B",
                )
            )
            await session.flush()
            session.add(
                Project(
                    id=project_b,
                    tenant_id=tenant_b,
                    workspace_id=workspace_b,
                    slug="project-b",
                    name="Project B",
                )
            )
            await session.commit()

    subjects = {name: spec[0] for name, spec in user_specs.items()}
    user_ids = {name: user.id for name, user in users.items()}
    tokens = {
        name: _token(
            private_key,
            subject=subjects[name],
            tenant_id=tenant_b if name == "tenant_admin_b" else tenant_a,
        )
        for name in user_specs
    }

    return Seed(
        tenant_a=tenant_a,
        tenant_b=tenant_b,
        org_a=org_a,
        workspace_a=workspace_a,
        project_a=project_a,
        org_b=org_b,
        workspace_b=workspace_b,
        project_b=project_b,
        user_ids=user_ids,
        subjects=subjects,
        tokens=tokens,
        session_factory=session_factory,
    )


@pytest.fixture
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    jwt_private_key: rsa.RSAPrivateKey,
) -> Seed:
    return await _seed_database(session_factory, jwt_private_key)


@pytest.fixture
async def api(
    seed: Seed,
    test_database_url: str,
    jwt_private_key: rsa.RSAPrivateKey,
    jwt_public_key: rsa.RSAPublicKey,
) -> ApiHarness:
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
        yield ApiHarness(
            client=client,
            seed=seed,
            app=application,
            private_key=jwt_private_key,
        )


async def test_health_is_public_and_api_requires_a_valid_token(
    api: ApiHarness,
    jwt_bad_private_key: rsa.RSAPrivateKey,
) -> None:
    live = await api.client.get("/health/live")
    assert live.status_code == 200

    missing = await api.client.get("/api/v1/me")
    assert missing.status_code == 401
    assert missing.json()["error"]["code"] == "unauthorized"
    assert missing.headers["www-authenticate"].startswith("Bearer")

    forged = _token(
        jwt_bad_private_key,
        subject=api.seed.subjects["tenant_admin_a"],
        tenant_id=api.seed.tenant_a,
    )
    invalid = await api.client.get("/api/v1/me", headers=_auth(forged))
    assert invalid.status_code == 401
    assert "invalid_token" in invalid.headers["www-authenticate"]


async def test_me_and_tenant_claim_mismatch(api: ApiHarness) -> None:
    response = await api.client.get("/api/v1/me", headers=_auth(api.seed.tokens["org_viewer_a"]))
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"].startswith("org-viewer-a-")
    assert any(
        item["scope_type"] == "organization"
        and item["scope_id"] == str(api.seed.org_a)
        and item["role"] == "viewer"
        for item in body["memberships"]
    )

    mismatched = _token(
        api.private_key,
        subject=api.seed.subjects["org_admin_a"],
        tenant_id=api.seed.tenant_b,
    )
    rejected = await api.client.get("/api/v1/me", headers=_auth(mismatched))
    assert rejected.status_code == 401


async def test_cross_tenant_resources_are_not_found(api: ApiHarness) -> None:
    response = await api.client.get(
        f"/api/v1/organizations/{api.seed.org_b}",
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
    )
    assert response.status_code == 404

    listing = await api.client.get(
        "/api/v1/organizations", headers=_auth(api.seed.tokens["tenant_admin_a"])
    )
    assert listing.status_code == 200
    ids = {item["id"] for item in listing.json()}
    assert str(api.seed.org_a) in ids
    assert str(api.seed.org_b) not in ids

    reverse = await api.client.get(
        f"/api/v1/organizations/{api.seed.org_a}",
        headers=_auth(api.seed.tokens["tenant_admin_b"]),
    )
    assert reverse.status_code == 404


async def test_role_enforcement_and_jwt_claims_ignored(api: ApiHarness) -> None:
    workspace_payload = {"slug": "viewer-workspace", "name": "Viewer Workspace"}
    denied = await api.client.post(
        f"/api/v1/organizations/{api.seed.org_a}/workspaces",
        json=workspace_payload,
        headers=_auth(api.seed.tokens["org_viewer_a"]),
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"

    created = await api.client.post(
        f"/api/v1/organizations/{api.seed.org_a}/workspaces",
        json=workspace_payload,
        headers=_auth(api.seed.tokens["org_editor_a"]),
    )
    assert created.status_code == 201

    project_payload = {"slug": "viewer-project", "name": "Viewer Project"}
    viewer_project = await api.client.post(
        f"/api/v1/workspaces/{api.seed.workspace_a}/projects",
        json=project_payload,
        headers=_auth(api.seed.tokens["workspace_viewer_a"]),
    )
    assert viewer_project.status_code == 403
    editor_project = await api.client.post(
        f"/api/v1/workspaces/{api.seed.workspace_a}/projects",
        json=project_payload,
        headers=_auth(api.seed.tokens["workspace_editor_a"]),
    )
    assert editor_project.status_code == 201

    claim_escalation = _token(
        api.private_key,
        subject=api.seed.subjects["org_viewer_a"],
        tenant_id=api.seed.tenant_a,
        extra={"role": "tenant_admin", "permissions": ["*"]},
    )
    claimed_admin = await api.client.post(
        "/api/v1/users",
        json={
            "subject": "attacker",
            "email": "attacker@example.com",
            "display_name": "Attacker",
        },
        headers=_auth(claim_escalation),
    )
    assert claimed_admin.status_code == 403

    provisioned = await api.client.post(
        "/api/v1/users",
        json={
            "subject": "new-user",
            "email": "new-user@example.com",
            "display_name": "New User",
        },
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
    )
    assert provisioned.status_code == 201


async def test_membership_grant_and_revoke(api: ApiHarness) -> None:
    grant = await api.client.post(
        f"/api/v1/organizations/{api.seed.org_a}/members",
        json={"user_id": str(api.seed.user_ids["outsider_a"]), "role": "viewer"},
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
    )
    assert grant.status_code == 200

    readable = await api.client.get(
        f"/api/v1/organizations/{api.seed.org_a}",
        headers=_auth(api.seed.tokens["outsider_a"]),
    )
    assert readable.status_code == 200

    revoked = await api.client.delete(
        f"/api/v1/organizations/{api.seed.org_a}/members/{api.seed.user_ids['outsider_a']}",
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
    )
    assert revoked.status_code == 204

    denied = await api.client.get(
        f"/api/v1/organizations/{api.seed.org_a}",
        headers=_auth(api.seed.tokens["outsider_a"]),
    )
    assert denied.status_code == 403


async def test_last_organization_admin_cannot_be_removed(api: ApiHarness) -> None:
    denied = await api.client.delete(
        f"/api/v1/organizations/{api.seed.org_a}/members/{api.seed.user_ids['org_admin_a']}",
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
    )
    assert denied.status_code == 409

    promote = await api.client.post(
        f"/api/v1/organizations/{api.seed.org_a}/members",
        json={"user_id": str(api.seed.user_ids["org_editor_a"]), "role": "admin"},
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
    )
    assert promote.status_code == 200

    removed = await api.client.delete(
        f"/api/v1/organizations/{api.seed.org_a}/members/{api.seed.user_ids['org_admin_a']}",
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
    )
    assert removed.status_code == 204


async def test_project_member_can_read_ancestors(api: ApiHarness) -> None:
    headers = _auth(api.seed.tokens["project_editor_a"])
    for path in (
        f"/api/v1/organizations/{api.seed.org_a}",
        f"/api/v1/workspaces/{api.seed.workspace_a}",
        f"/api/v1/projects/{api.seed.project_a}",
    ):
        response = await api.client.get(path, headers=headers)
        assert response.status_code == 200, path


async def test_abac_confidential_and_archived(api: ApiHarness) -> None:
    admin_headers = _auth(api.seed.tokens["org_admin_a"])
    editor_headers = _auth(api.seed.tokens["project_editor_a"])
    project_path = f"/api/v1/projects/{api.seed.project_a}"

    editor_classification = await api.client.patch(
        project_path,
        json={"classification": "confidential"},
        headers=editor_headers,
    )
    assert editor_classification.status_code == 403
    editor_status = await api.client.patch(
        project_path, json={"status": "archived"}, headers=editor_headers
    )
    assert editor_status.status_code == 403

    confidential = await api.client.patch(
        project_path, json={"classification": "confidential"}, headers=admin_headers
    )
    assert confidential.status_code == 200

    denied = await api.client.patch(
        project_path, json={"name": "Editor Change"}, headers=editor_headers
    )
    assert denied.status_code == 403

    allowed = await api.client.patch(
        project_path, json={"name": "Admin Change"}, headers=admin_headers
    )
    assert allowed.status_code == 200

    archived = await api.client.patch(
        project_path, json={"status": "archived"}, headers=admin_headers
    )
    assert archived.status_code == 200

    archived_denied = await api.client.patch(
        project_path, json={"name": "Editor Change 2"}, headers=editor_headers
    )
    assert archived_denied.status_code == 403

    restored = await api.client.patch(
        project_path, json={"status": "active"}, headers=admin_headers
    )
    assert restored.status_code == 200


async def test_workspace_status_changes_require_admin(api: ApiHarness) -> None:
    workspace_path = f"/api/v1/workspaces/{api.seed.workspace_a}"
    editor_headers = _auth(api.seed.tokens["workspace_editor_a"])
    admin_headers = _auth(api.seed.tokens["workspace_admin_a"])

    denied = await api.client.patch(
        workspace_path, json={"status": "archived"}, headers=editor_headers
    )
    assert denied.status_code == 403

    archived = await api.client.patch(
        workspace_path, json={"status": "archived"}, headers=admin_headers
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"

    restored = await api.client.patch(
        workspace_path, json={"status": "active"}, headers=admin_headers
    )
    assert restored.status_code == 200


async def test_audit_records_success_and_denial(api: ApiHarness) -> None:
    denied = await api.client.post(
        f"/api/v1/organizations/{api.seed.org_a}/workspaces",
        json={"slug": "denied-workspace", "name": "Denied"},
        headers=_auth(api.seed.tokens["org_viewer_a"]),
    )
    assert denied.status_code == 403

    created = await api.client.post(
        "/api/v1/organizations",
        json={"slug": "audit-org", "name": "Audit Org"},
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
    )
    assert created.status_code == 201

    audit = await api.client.get(
        "/api/v1/audit-events",
        headers=_auth(api.seed.tokens["tenant_admin_a"]),
        params={"limit": 100},
    )
    assert audit.status_code == 200
    events = audit.json()
    actions = {event["action"] for event in events}
    assert "authorization.denied" in actions
    assert "organization.created" in actions
    denial = next(event for event in events if event["action"] == "authorization.denied")
    assert denial["outcome"] == "denied"
    assert denial["details"]["permission"] == "workspace:create"

    forbidden = await api.client.get(
        "/api/v1/audit-events",
        headers=_auth(api.seed.tokens["org_admin_a"]),
    )
    assert forbidden.status_code == 403


async def test_suspended_tenant_blocks_requests(api: ApiHarness) -> None:
    async with api.seed.session_factory() as session:
        tenant = await session.get(Tenant, api.seed.tenant_a)
        assert tenant is not None
        tenant.status = "suspended"
        await session.commit()

    response = await api.client.get("/api/v1/me", headers=_auth(api.seed.tokens["tenant_admin_a"]))
    assert response.status_code == 403

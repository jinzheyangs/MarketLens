from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli import create_tenant, provision_user
from app.core.context import tenant_context
from app.db.models import AuditEvent, User
from app.schemas.users import UserProvisionRequest

pytestmark = pytest.mark.integration

ISSUER = "https://idp.marketlens.test"


async def test_bootstrap_tenant_and_tenant_admin(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    slug = f"bootstrap-{uuid4().hex[:8]}"
    tenant = await create_tenant(session_factory, slug=slug, name="Bootstrap Tenant")
    assert tenant.slug == slug

    payload = UserProvisionRequest(
        subject="bootstrap-admin",
        email="bootstrap-admin@example.com",
        display_name="Bootstrap Admin",
        tenant_role="tenant_admin",
    )
    user = await provision_user(
        session_factory,
        tenant_slug=slug,
        payload=payload,
        issuer=ISSUER,
    )
    assert user.tenant_id == tenant.id
    assert user.tenant_role == "tenant_admin"

    with tenant_context(tenant.id):
        async with session_factory() as session:
            loaded = await session.scalar(select(User).where(User.id == user.id))
            assert loaded is not None
            assert loaded.email == "bootstrap-admin@example.com"
            actions = set(
                (
                    await session.scalars(
                        select(AuditEvent.action).where(AuditEvent.tenant_id == tenant.id)
                    )
                ).all()
            )
    assert {"tenant.created", "user.provisioned"} <= actions

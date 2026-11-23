from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import String, inspect, select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from app.core.context import tenant_context
from app.core.errors import TenantContextError, TenantIsolationError
from app.db.base import Base
from app.db.mixins import TenantScopedMixin, UUIDPrimaryKeyMixin
from app.db.models import Tenant
from app.db.tenant_scope import tenant_scoped_select

pytestmark = pytest.mark.integration


class TenantScopedProbe(UUIDPrimaryKeyMixin, TenantScopedMixin, Base):
    """Test-only table used to exercise the ORM tenant guard and query helper."""

    __tablename__ = "integration_tenant_scoped_probe"

    label: Mapped[str] = mapped_column(String(64), nullable=False)


def _create_probe(connection: Connection) -> None:
    TenantScopedProbe.__table__.create(connection, checkfirst=True)


def _drop_probe(connection: Connection) -> None:
    TenantScopedProbe.__table__.drop(connection, checkfirst=True)


@pytest.fixture
async def probe_engine(engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    async with engine.begin() as connection:
        await connection.run_sync(_create_probe)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(_drop_probe)


@pytest.fixture
def session_factory(probe_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(probe_engine, expire_on_commit=False, autoflush=False)


async def _create_tenant(factory: async_sessionmaker[AsyncSession], label: str) -> UUID:
    tenant_id = uuid4()
    async with factory() as session:
        session.add(Tenant(id=tenant_id, slug=f"{label}-{tenant_id.hex[:8]}", name=label))
        await session.commit()
    return tenant_id


def _inspect_tenants(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    return {
        "columns": {column["name"]: column for column in inspector.get_columns("tenants")},
        "primary_key": inspector.get_pk_constraint("tenants"),
        "unique": inspector.get_unique_constraints("tenants"),
        "checks": inspector.get_check_constraints("tenants"),
        "comment": inspector.get_table_comment("tenants"),
        "has_alembic_version": inspector.has_table("alembic_version"),
    }


async def test_migration_creates_tenants_table(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        schema = await connection.run_sync(_inspect_tenants)

    assert schema["has_alembic_version"] is True
    assert set(schema["columns"]) == {
        "id",
        "slug",
        "name",
        "status",
        "created_at",
        "updated_at",
    }
    assert schema["columns"]["slug"]["nullable"] is False
    assert schema["columns"]["status"]["nullable"] is False
    assert schema["primary_key"]["name"] == "pk_tenants"
    assert any(item["name"] == "uq_tenants_slug" for item in schema["unique"])
    assert any(item["name"] == "ck_tenants_status_valid" for item in schema["checks"])
    assert "stage 2" in schema["comment"]["text"]


async def test_tenant_registry_crud(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    async with session_factory() as session:
        session.add(Tenant(id=tenant_id, slug=f"crud-{tenant_id.hex[:8]}", name="CRUD"))
        await session.commit()

    async with session_factory() as session:
        found = await session.get(Tenant, tenant_id)
        assert found is not None
        assert found.status == "active"


async def test_tenant_scoped_insert_without_context_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        session.add(TenantScopedProbe(label="unbound"))
        with pytest.raises(TenantContextError):
            await session.commit()
        await session.rollback()


async def test_tenant_scoped_insert_is_bound_to_context(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _create_tenant(session_factory, "bind")

    with tenant_context(tenant_id):
        async with session_factory() as session:
            probe = TenantScopedProbe(label="bound")
            session.add(probe)
            await session.commit()
            assert probe.tenant_id == tenant_id


async def test_cross_tenant_insert_is_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(session_factory, "cross-a")
    tenant_b = await _create_tenant(session_factory, "cross-b")

    with tenant_context(tenant_a):
        async with session_factory() as session:
            session.add(TenantScopedProbe(tenant_id=tenant_b, label="wrong-tenant"))
            with pytest.raises(TenantIsolationError):
                await session.commit()
            await session.rollback()


async def test_tenant_scoped_select_cannot_see_other_tenants(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_a = await _create_tenant(session_factory, "select-a")
    tenant_b = await _create_tenant(session_factory, "select-b")

    for tenant_id, label in ((tenant_a, "a-row"), (tenant_b, "b-row")):
        with tenant_context(tenant_id):
            async with session_factory() as session:
                session.add(TenantScopedProbe(label=label))
                await session.commit()

    with tenant_context(tenant_a):
        async with session_factory() as session:
            rows = (await session.execute(tenant_scoped_select(TenantScopedProbe))).scalars().all()
            assert [row.label for row in rows] == ["a-row"]
            assert all(row.tenant_id == tenant_a for row in rows)

    # A query helper without context fails closed instead of returning all rows.
    with pytest.raises(TenantContextError):
        tenant_scoped_select(TenantScopedProbe)


async def test_tenant_guard_does_not_block_root_registry(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid4()
    async with session_factory() as session:
        session.add(Tenant(id=tenant_id, slug=f"root-{tenant_id.hex[:8]}", name="Root"))
        await session.commit()
        loaded = await session.scalar(select(Tenant).where(Tenant.id == tenant_id))
        assert loaded is not None

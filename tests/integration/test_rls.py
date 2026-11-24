from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.context import tenant_context
from app.db.models import (
    Document,
    Organization,
    Report,
    ResearchReview,
    ResearchRun,
    Tenant,
    Workspace,
)

pytestmark = pytest.mark.integration

_RLS_TABLES = {
    "users",
    "organizations",
    "workspaces",
    "projects",
    "organization_memberships",
    "workspace_memberships",
    "project_memberships",
    "audit_events",
    "documents",
    "document_versions",
    "document_chunks",
    "collector_domain_limits",
    "web_sources",
    "collection_tasks",
    "source_snapshots",
    "research_runs",
    "research_checkpoints",
    "research_findings",
    "research_tool_calls",
    "research_sources",
    "research_evidence",
    "research_claims",
    "research_citations",
    "research_reviews",
    "reports",
    "report_versions",
    "report_export_jobs",
    "domain_events",
    "event_consumer_failures",
    "processed_events",
    "notification_channels",
    "monitor_subscriptions",
    "monitor_alert_rules",
    "monitor_runs",
    "monitor_seen_sources",
    "monitor_changes",
    "notification_deliveries",
    "project_comments",
    "approval_requests",
    "tenant_quotas",
    "tenant_domain_policies",
}


async def _create_tenant(
    session_factory: async_sessionmaker[AsyncSession],
    label: str,
) -> UUID:
    tenant_id = uuid4()
    async with session_factory() as session:
        session.add(Tenant(id=tenant_id, slug=f"{label}-{tenant_id.hex[:8]}", name=label))
        await session.commit()
    return tenant_id


async def _create_org(
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: UUID,
    slug: str,
) -> UUID:
    organization_id = uuid4()
    with tenant_context(tenant_id):
        async with session_factory() as session:
            session.add(
                Organization(
                    id=organization_id,
                    tenant_id=tenant_id,
                    slug=slug,
                    name=slug,
                )
            )
            await session.commit()
    return organization_id


@pytest.fixture
async def tenants_and_orgs(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID, UUID]:
    tenant_a = await _create_tenant(session_factory, "rls-a")
    tenant_b = await _create_tenant(session_factory, "rls-b")
    org_a = await _create_org(session_factory, tenant_a, "org-a")
    org_b = await _create_org(session_factory, tenant_b, "org-b")
    return tenant_a, tenant_b, org_a, org_b


@pytest.fixture
async def non_superuser_role(engine: AsyncEngine) -> AsyncIterator[str]:
    role = f"rls_probe_{uuid4().hex[:8]}"
    async with engine.begin() as connection:
        await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
        await connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        await connection.execute(
            text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}"')
        )
    try:
        yield role
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP OWNED BY "{role}"'))
            await connection.execute(text(f'DROP ROLE "{role}"'))


async def test_rls_policies_exist_for_all_tenant_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT tablename FROM pg_policies WHERE policyname = 'tenant_isolation'")
        )
        tables = {row[0] for row in rows}
    assert tables == _RLS_TABLES


async def test_session_binds_tenant_guc(
    session_factory: async_sessionmaker[AsyncSession],
    tenants_and_orgs: tuple[UUID, UUID, UUID, UUID],
) -> None:
    tenant_a, _, _, _ = tenants_and_orgs
    with tenant_context(tenant_a):
        async with session_factory() as session:
            value = await session.scalar(text("SELECT current_setting('app.tenant_id', true)"))
            assert value == str(tenant_a)

    async with session_factory() as session:
        unset = await session.scalar(text("SELECT current_setting('app.tenant_id', true)"))
    assert unset in (None, "")


async def test_non_superuser_only_sees_current_tenant(
    engine: AsyncEngine,
    tenants_and_orgs: tuple[UUID, UUID, UUID, UUID],
    non_superuser_role: str,
) -> None:
    tenant_a, _, org_a, _ = tenants_and_orgs
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(text(f'SET LOCAL ROLE "{non_superuser_role}"'))
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_a)},
        )
        rows = (
            (await connection.execute(text("SELECT id FROM organizations ORDER BY id")))
            .scalars()
            .all()
        )
        await transaction.rollback()
    assert rows == [org_a]


async def test_non_superuser_without_tenant_context_sees_nothing(
    engine: AsyncEngine,
    tenants_and_orgs: tuple[UUID, UUID, UUID, UUID],
    non_superuser_role: str,
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(text(f'SET LOCAL ROLE "{non_superuser_role}"'))
        rows = (await connection.execute(text("SELECT id FROM organizations"))).scalars().all()
        await transaction.rollback()
    assert rows == []


async def test_rls_rejects_cross_tenant_insert(
    engine: AsyncEngine,
    tenants_and_orgs: tuple[UUID, UUID, UUID, UUID],
    non_superuser_role: str,
) -> None:
    tenant_a, tenant_b, _, _ = tenants_and_orgs
    with pytest.raises(DBAPIError):
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text(f'SET LOCAL ROLE "{non_superuser_role}"'))
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_a)},
            )
            await connection.execute(
                text(
                    "INSERT INTO organizations "
                    "(id, tenant_id, slug, name, status, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :slug, :name, 'active', now(), now())"
                ),
                {
                    "id": uuid4(),
                    "tenant_id": str(tenant_b),
                    "slug": "cross-tenant",
                    "name": "Cross Tenant",
                },
            )
            await transaction.commit()


async def test_document_tables_are_rls_protected(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tenants_and_orgs: tuple[UUID, UUID, UUID, UUID],
    non_superuser_role: str,
) -> None:
    tenant_a, tenant_b, org_a, org_b = tenants_and_orgs
    workspace_a = uuid4()
    workspace_b = uuid4()
    document_a = uuid4()
    document_b = uuid4()

    with tenant_context(tenant_a):
        async with session_factory() as session:
            session.add(
                Workspace(
                    id=workspace_a,
                    tenant_id=tenant_a,
                    organization_id=org_a,
                    slug=f"rls-doc-ws-a-{workspace_a.hex[:8]}",
                    name="RLS A",
                )
            )
            await session.flush()
            session.add(
                Document(
                    id=document_a,
                    tenant_id=tenant_a,
                    workspace_id=workspace_a,
                    title="Tenant A document",
                )
            )
            await session.commit()

    with tenant_context(tenant_b):
        async with session_factory() as session:
            session.add(
                Workspace(
                    id=workspace_b,
                    tenant_id=tenant_b,
                    organization_id=org_b,
                    slug=f"rls-doc-ws-b-{workspace_b.hex[:8]}",
                    name="RLS B",
                )
            )
            await session.flush()
            session.add(
                Document(
                    id=document_b,
                    tenant_id=tenant_b,
                    workspace_id=workspace_b,
                    title="Tenant B document",
                )
            )
            await session.commit()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(text(f'SET LOCAL ROLE "{non_superuser_role}"'))
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_a)},
        )
        rows = (await connection.execute(text("SELECT id FROM documents"))).scalars().all()
        await transaction.rollback()

    assert rows == [document_a]


async def test_composite_foreign_key_blocks_cross_tenant_parent(
    session_factory: async_sessionmaker[AsyncSession],
    tenants_and_orgs: tuple[UUID, UUID, UUID, UUID],
) -> None:
    tenant_a, _, _, org_b = tenants_and_orgs
    with pytest.raises(IntegrityError), tenant_context(tenant_a):
        async with session_factory() as session:
            session.add(
                Workspace(
                    id=uuid4(),
                    tenant_id=tenant_a,
                    organization_id=org_b,
                    slug="cross-parent",
                    name="Cross Parent",
                )
            )
            await session.commit()


async def test_research_runs_are_rls_protected(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    tenants_and_orgs: tuple[UUID, UUID, UUID, UUID],
    non_superuser_role: str,
) -> None:
    tenant_a, tenant_b, org_a, org_b = tenants_and_orgs
    workspace_a = uuid4()
    workspace_b = uuid4()
    run_a = uuid4()
    run_b = uuid4()
    review_a = uuid4()
    review_b = uuid4()
    report_a = uuid4()
    report_b = uuid4()

    with tenant_context(tenant_a):
        async with session_factory() as session:
            session.add(
                Workspace(
                    id=workspace_a,
                    tenant_id=tenant_a,
                    organization_id=org_a,
                    slug=f"rls-research-ws-a-{workspace_a.hex[:8]}",
                    name="RLS Research A",
                )
            )
            await session.flush()
            session.add(
                ResearchRun(
                    id=run_a,
                    tenant_id=tenant_a,
                    workspace_id=workspace_a,
                    objective="Tenant A research",
                    model_provider="deterministic",
                    model_name="test",
                    graph_version="stage5-v1",
                    max_rounds=1,
                    max_attempts=1,
                )
            )
            await session.flush()
            session.add(
                ResearchReview(
                    id=review_a,
                    tenant_id=tenant_a,
                    root_run_id=run_a,
                    workspace_id=workspace_a,
                    max_attempts=1,
                    rules_version="stage6-v1",
                )
            )
            await session.flush()
            session.add(
                Report(
                    id=report_a,
                    tenant_id=tenant_a,
                    root_run_id=run_a,
                    workspace_id=workspace_a,
                )
            )
            await session.commit()

    with tenant_context(tenant_b):
        async with session_factory() as session:
            session.add(
                Workspace(
                    id=workspace_b,
                    tenant_id=tenant_b,
                    organization_id=org_b,
                    slug=f"rls-research-ws-b-{workspace_b.hex[:8]}",
                    name="RLS Research B",
                )
            )
            await session.flush()
            session.add(
                ResearchRun(
                    id=run_b,
                    tenant_id=tenant_b,
                    workspace_id=workspace_b,
                    objective="Tenant B research",
                    model_provider="deterministic",
                    model_name="test",
                    graph_version="stage5-v1",
                    max_rounds=1,
                    max_attempts=1,
                )
            )
            await session.flush()
            session.add(
                ResearchReview(
                    id=review_b,
                    tenant_id=tenant_b,
                    root_run_id=run_b,
                    workspace_id=workspace_b,
                    max_attempts=1,
                    rules_version="stage6-v1",
                )
            )
            await session.flush()
            session.add(
                Report(
                    id=report_b,
                    tenant_id=tenant_b,
                    root_run_id=run_b,
                    workspace_id=workspace_b,
                )
            )
            await session.commit()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(text(f'SET LOCAL ROLE "{non_superuser_role}"'))
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_a)},
        )
        rows = (await connection.execute(text("SELECT id FROM research_runs"))).scalars().all()
        report_rows = (await connection.execute(text("SELECT id FROM reports"))).scalars().all()
        review_rows = (
            (await connection.execute(text("SELECT id FROM research_reviews"))).scalars().all()
        )
        await transaction.rollback()

    assert rows == [run_a]
    assert report_rows == [report_a]
    assert review_rows == [review_a]

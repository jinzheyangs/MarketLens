from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

from app.agent.enums import FindingSourceKind
from app.agent.tools import ResearchToolbox
from app.auth.enums import ScopeRole, TenantRole, TenantStatus
from app.auth.principal import Principal
from app.rag.types import DocumentAccessScope, SearchResult
from tests.conftest import make_settings


class FakeRagServices:
    async def search(
        self,
        *,
        scope: DocumentAccessScope,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        del scope, query, limit
        return [
            SearchResult(
                chunk_id=uuid4(),
                document_id=uuid4(),
                version_id=uuid4(),
                version_number=2,
                document_title="Board memo",
                content="Enterprise retention improved in the latest quarter.",
                page_number=4,
                paragraph_number=1,
                start_offset=220,
                end_offset=270,
                score=0.9,
                vector_rank=1,
                keyword_rank=1,
                observed_at=datetime(2026, 11, 1, tzinfo=UTC),
            )
        ]


async def test_knowledge_tool_maps_authorized_rag_locations() -> None:
    tenant_id = uuid4()
    workspace_id = uuid4()
    principal = Principal(
        user_id=uuid4(),
        tenant_id=tenant_id,
        tenant_slug="acme",
        tenant_name="Acme",
        tenant_status=TenantStatus.ACTIVE,
        issuer="https://idp.test",
        subject="user-1",
        email="user@example.test",
        display_name="User",
        tenant_role=TenantRole.MEMBER,
        organization_roles={},
        workspace_roles={workspace_id: ScopeRole.EDITOR},
        project_roles={},
        organization_ids=frozenset(),
        workspace_ids=frozenset({workspace_id}),
        project_ids=frozenset(),
    )
    scope = DocumentAccessScope(
        workspace_id=workspace_id,
        allowed_project_ids=None,
        include_workspace_documents=True,
        selected_project_id=None,
    )
    toolbox = ResearchToolbox(
        settings=make_settings(log_level="WARNING"),
        session_factory=cast(Any, None),
        principal=principal,
        organization_id=uuid4(),
        workspace_id=workspace_id,
        project_id=None,
        run_id=uuid4(),
        document_scope=scope,
        rag_services=cast(Any, FakeRagServices()),
        collector_services=None,
    )

    evidence, leads = await toolbox._knowledge_search("enterprise retention")

    assert leads == []
    assert len(evidence) == 1
    item = evidence[0]
    assert item.source_kind is FindingSourceKind.DOCUMENT
    assert item.document_version_id is not None
    assert item.page_number == 4
    assert item.paragraph_number == 1
    assert item.start_offset == 220
    assert item.end_offset == 270
    assert item.observed_at == datetime(2026, 11, 1, tzinfo=UTC)

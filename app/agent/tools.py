"""Read-only research tool registry with explicit least-privilege scopes.

No write, administration, collection-mutation, or arbitrary HTTP tool is exposed
to an agent. Tool arguments are bounded and persisted as a trace; tool output is
treated as untrusted evidence and later checked against model citations.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.enums import (
    AgentRole,
    FindingSourceKind,
    ResearchToolName,
    ToolCallStatus,
)
from app.agent.errors import ToolExecutionError, ToolUnavailableError
from app.agent.evidence import EvidenceItem, SearchLead, ToolCollection
from app.auth.enums import ScopeRole
from app.auth.principal import Principal
from app.collector.errors import CollectorError
from app.collector.network import validate_url
from app.collector.service import CollectorServices, filter_search_hits, require_search_provider
from app.core.config import Settings
from app.db.models import ResearchToolCall, SourceSnapshot, WebSource
from app.db.tenant_scope import tenant_scoped_select
from app.rag.errors import RagError
from app.rag.types import DocumentAccessScope
from app.services.rag import RagServices

_TOKEN_PATTERN = re.compile(r"[^\W_]{2,}", re.UNICODE)
_EDITOR_ROLES = frozenset({ScopeRole.EDITOR, ScopeRole.ADMIN})


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_tokens(question: str) -> list[str]:
    tokens: list[str] = []
    for token in _TOKEN_PATTERN.findall(question):
        candidate = token[:40]
        if candidate not in tokens:
            tokens.append(candidate)
        if len(tokens) >= 6:
            break
    return tokens


def _evidence_id(kind: FindingSourceKind, identifier: UUID) -> str:
    return f"{kind.value}:{identifier}"


class ResearchToolbox:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        principal: Principal,
        organization_id: UUID,
        workspace_id: UUID,
        project_id: UUID | None,
        run_id: UUID,
        document_scope: DocumentAccessScope | None,
        rag_services: RagServices | None,
        collector_services: CollectorServices | None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._principal = principal
        self._organization_id = organization_id
        self._workspace_id = workspace_id
        self._project_id = project_id
        self._run_id = run_id
        self._document_scope = document_scope
        self._rag_services = rag_services
        self._collector_services = collector_services

        org_role = principal.organization_roles.get(organization_id)
        workspace_role = principal.workspace_roles.get(workspace_id)
        self._workspace_access = (
            principal.is_tenant_admin or org_role is not None or workspace_role is not None
        )
        self._workspace_mutation_access = (
            principal.is_tenant_admin
            or (org_role is not None and org_role in _EDITOR_ROLES)
            or (workspace_role is not None and workspace_role in _EDITOR_ROLES)
        )
        available: set[ResearchToolName] = set()
        if rag_services is not None and document_scope is not None:
            available.add(ResearchToolName.KNOWLEDGE_SEARCH)
        if self._workspace_access:
            available.add(ResearchToolName.SOURCE_SEARCH)
            available.add(ResearchToolName.SOURCE_READ)
        if (
            self._workspace_mutation_access
            and collector_services is not None
            and collector_services.search_provider is not None
        ):
            available.add(ResearchToolName.WEB_SEARCH)
        self._available_tools = frozenset(available)

    @property
    def available_tools(self) -> frozenset[ResearchToolName]:
        return self._available_tools

    async def collect(
        self,
        *,
        role: AgentRole,
        question: str,
        tool_call_budget: int,
    ) -> ToolCollection:
        collection = ToolCollection()
        call_limit = min(tool_call_budget, self._settings.agent_max_tool_calls_per_task)
        if call_limit <= 0:
            collection.errors.append(
                {"code": "tool_budget_exhausted", "message": "No tool calls remain."}
            )
            return collection

        order = (
            ResearchToolName.KNOWLEDGE_SEARCH,
            ResearchToolName.SOURCE_SEARCH,
            ResearchToolName.WEB_SEARCH,
        )
        for tool_name in order:
            if collection.tool_calls >= call_limit:
                break
            if tool_name not in self._available_tools:
                continue
            await self._execute(
                collection,
                role=role,
                tool_name=tool_name,
                question=question,
            )

        collection.deduplicate(
            max_evidence=self._settings.agent_max_evidence_items,
            max_leads=self._settings.agent_max_leads,
        )
        return collection

    async def _execute(
        self,
        collection: ToolCollection,
        *,
        role: AgentRole,
        tool_name: ResearchToolName,
        question: str,
    ) -> None:
        started = time.perf_counter()
        collection.tool_calls += 1
        try:
            evidence, leads = await self._invoke(tool_name, question=question)
        except (ToolExecutionError, ToolUnavailableError) as exc:
            await self._record(
                role=role,
                tool_name=tool_name,
                status=ToolCallStatus.FAILED,
                query=question,
                source_id=None,
                result_count=0,
                error_code=exc.code,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            collection.errors.append({"code": exc.code, "message": exc.safe_message})
            return
        except CollectorError:
            await self._record(
                role=role,
                tool_name=tool_name,
                status=ToolCallStatus.FAILED,
                query=question,
                source_id=None,
                result_count=0,
                error_code="collector_error",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            collection.errors.append(
                {"code": "collector_error", "message": "External search failed."}
            )
            return
        except RagError:
            await self._record(
                role=role,
                tool_name=tool_name,
                status=ToolCallStatus.FAILED,
                query=question,
                source_id=None,
                result_count=0,
                error_code="rag_error",
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
            collection.errors.append({"code": "rag_error", "message": "Knowledge search failed."})
            return

        await self._record(
            role=role,
            tool_name=tool_name,
            status=ToolCallStatus.SUCCEEDED,
            query=question,
            source_id=None,
            result_count=len(evidence) + len(leads),
            error_code=None,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
        collection.evidence.extend(evidence)
        collection.leads.extend(leads)

    async def _invoke(
        self,
        tool_name: ResearchToolName,
        *,
        question: str,
    ) -> tuple[list[EvidenceItem], list[SearchLead]]:
        if tool_name is ResearchToolName.KNOWLEDGE_SEARCH:
            return await self._knowledge_search(question)
        if tool_name is ResearchToolName.SOURCE_SEARCH:
            return await self._source_search(question)
        if tool_name is ResearchToolName.WEB_SEARCH:
            return await self._web_search(question)
        raise ToolUnavailableError

    async def _knowledge_search(
        self,
        question: str,
    ) -> tuple[list[EvidenceItem], list[SearchLead]]:
        if self._rag_services is None or self._document_scope is None:
            raise ToolUnavailableError
        results = await self._rag_services.search(
            scope=self._document_scope,
            query=question,
            limit=self._settings.agent_max_evidence_items,
        )
        fallback_observed_at = datetime.now(UTC)
        items = [
            EvidenceItem(
                evidence_id=_evidence_id(FindingSourceKind.DOCUMENT, result.chunk_id),
                source_kind=FindingSourceKind.DOCUMENT,
                source_id=result.document_id,
                title=result.document_title,
                text=result.content[: self._settings.agent_max_evidence_chars],
                observed_at=result.observed_at or fallback_observed_at,
                document_id=result.document_id,
                document_version_id=result.version_id,
                chunk_id=result.chunk_id,
                page_number=result.page_number,
                paragraph_number=result.paragraph_number,
                start_offset=result.start_offset,
                end_offset=result.end_offset,
            )
            for result in results
        ]
        return items, []

    async def _source_search(
        self,
        question: str,
    ) -> tuple[list[EvidenceItem], list[SearchLead]]:
        tokens = _query_tokens(question)
        if not tokens:
            raise ToolExecutionError
        conditions = [
            SourceSnapshot.extracted_text.ilike(
                f"%{_escape_like(token)}%",
                escape="\\",
            )
            for token in tokens
        ]
        async with self._session_factory() as session:
            statement = (
                select(WebSource, SourceSnapshot)
                .join(
                    SourceSnapshot,
                    SourceSnapshot.id == WebSource.latest_snapshot_id,
                )
                .where(
                    WebSource.tenant_id == self._principal.tenant_id,
                    SourceSnapshot.tenant_id == self._principal.tenant_id,
                    WebSource.workspace_id == self._workspace_id,
                    WebSource.status == "active",
                    SourceSnapshot.status == "active",
                    SourceSnapshot.extracted_text.is_not(None),
                    or_(*conditions),
                )
                .order_by(WebSource.last_fetched_at.desc().nullslast(), WebSource.id)
                .limit(self._settings.agent_max_evidence_items * 3)
            )
            rows = (await session.execute(statement)).all()
        return [self._source_evidence(source, snapshot) for source, snapshot in rows], []

    async def _source_read(
        self,
        source_id: UUID,
    ) -> tuple[list[EvidenceItem], list[SearchLead]]:
        async with self._session_factory() as session:
            source = await session.scalar(
                tenant_scoped_select(WebSource).where(
                    WebSource.id == source_id,
                    WebSource.workspace_id == self._workspace_id,
                    WebSource.status == "active",
                )
            )
            if source is None or source.latest_snapshot_id is None:
                raise ToolExecutionError
            snapshot = await session.scalar(
                tenant_scoped_select(SourceSnapshot).where(
                    SourceSnapshot.id == source.latest_snapshot_id,
                    SourceSnapshot.workspace_id == self._workspace_id,
                    SourceSnapshot.status == "active",
                )
            )
        if snapshot is None:
            raise ToolExecutionError
        return [self._source_evidence(source, snapshot)], []

    async def _web_search(
        self,
        question: str,
    ) -> tuple[list[EvidenceItem], list[SearchLead]]:
        if self._collector_services is None:
            raise ToolUnavailableError
        provider = require_search_provider(self._collector_services)
        raw_hits = await provider.search(
            question,
            limit=self._settings.agent_max_evidence_items + self._settings.agent_max_leads,
        )
        hits = filter_search_hits(
            raw_hits,
            settings=self._settings,
            limit=self._settings.agent_max_evidence_items + self._settings.agent_max_leads,
        )
        evidence: list[EvidenceItem] = []
        leads: list[SearchLead] = []
        async with self._session_factory() as session:
            for hit in hits:
                safe = validate_url(hit.url, self._settings)
                source = await session.scalar(
                    tenant_scoped_select(WebSource).where(
                        WebSource.workspace_id == self._workspace_id,
                        WebSource.url_hash == safe.url_hash,
                        WebSource.status == "active",
                    )
                )
                snapshot = None
                if source is not None and source.latest_snapshot_id is not None:
                    snapshot = await session.scalar(
                        tenant_scoped_select(SourceSnapshot).where(
                            SourceSnapshot.id == source.latest_snapshot_id,
                            SourceSnapshot.status == "active",
                        )
                    )
                if source is not None and snapshot is not None:
                    evidence.append(self._source_evidence(source, snapshot))
                else:
                    leads.append(
                        SearchLead(
                            url=safe.url,
                            title=hit.title,
                            snippet=(hit.snippet or "")[:500] or None,
                        )
                    )
        return evidence, leads

    def _source_evidence(
        self,
        source: WebSource,
        snapshot: SourceSnapshot,
    ) -> EvidenceItem:
        return EvidenceItem(
            evidence_id=_evidence_id(FindingSourceKind.EXTERNAL_SOURCE, snapshot.id),
            source_kind=FindingSourceKind.EXTERNAL_SOURCE,
            source_id=source.id,
            title=(source.title or source.domain)[:500],
            text=(snapshot.extracted_text or "")[: self._settings.agent_max_evidence_chars],
            observed_at=source.last_fetched_at or snapshot.fetched_at,
            source_snapshot_id=snapshot.id,
            source_url=source.url,
        )

    async def _record(
        self,
        *,
        role: AgentRole,
        tool_name: ResearchToolName,
        status: ToolCallStatus,
        query: str,
        source_id: UUID | None,
        result_count: int,
        error_code: str | None,
        latency_ms: float,
    ) -> None:
        arguments: dict[str, Any] = {"query": query[:500]}
        if source_id is not None:
            arguments = {"source_id": str(source_id)}
        async with self._session_factory() as session, session.begin():
            session.add(
                ResearchToolCall(
                    tenant_id=self._principal.tenant_id,
                    run_id=self._run_id,
                    agent_role=role.value,
                    tool_name=tool_name.value,
                    status=status.value,
                    arguments=arguments,
                    result_count=result_count,
                    error_code=error_code,
                    latency_ms=latency_ms,
                )
            )
            await session.flush()


def accessible_tool_names(toolbox: ResearchToolbox) -> Sequence[str]:
    return sorted(tool.value for tool in toolbox.available_tools)


__all__ = ["ResearchToolbox", "accessible_tool_names"]

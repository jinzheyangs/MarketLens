from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.enums import AgentRole, ResearchToolName
from app.agent.evidence import EvidenceItem
from app.agent.providers import AnalysisResult, DeterministicResearchModel
from app.agent.runner import ResearchRunner
from app.agent.service import AgentServices
from app.agent.tools import ResearchToolbox
from app.auth.oidc import JWTVerifier
from app.auth.principal import resolve_principal_for_user
from app.collector.search import SearchHit
from app.core.context import tenant_context
from app.db.models import DomainEvent, SourceSnapshot, WebSource, WorkspaceMembership
from app.factory import create_app
from app.reporting.runner import ExportRunner, ReviewRunner
from app.reporting.service import ReviewServices
from tests.conftest import make_settings, open_client
from tests.integration.test_api_stage2 import (
    AUDIENCE,
    ISSUER,
    Seed,
    _auth,
    _seed_database,
)

pytestmark = pytest.mark.integration


class FakeSearchProvider:
    def __init__(self, *, url: str) -> None:
        self.url = url

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        del query
        return [
            SearchHit(url=self.url, title="Market report", snippet="Revenue grew"),
            SearchHit(
                url="https://uncollected.example.test/lead",
                title="Uncollected lead",
                snippet="Not yet collected",
            ),
            SearchHit(url="http://127.0.0.1/private", title="Private", snippet=None),
        ][:limit]

    async def aclose(self) -> None:
        return None


class BlockingModel(DeterministicResearchModel):
    def __init__(self) -> None:
        super().__init__(model="blocking-test")
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def analyze(
        self,
        *,
        objective: str,
        role: AgentRole,
        question: str,
        evidence: Sequence[EvidenceItem],
        max_findings: int,
        max_output_tokens: int,
    ) -> AnalysisResult:
        self.started.set()
        await self.release.wait()
        return await super().analyze(
            objective=objective,
            role=role,
            question=question,
            evidence=evidence,
            max_findings=max_findings,
            max_output_tokens=max_output_tokens,
        )


@dataclass
class ResearchHarness:
    client: AsyncClient
    seed: Seed
    app: FastAPI
    services: AgentServices
    review_services: ReviewServices
    session_factory: async_sessionmaker[AsyncSession]
    source_id: UUID


@pytest.fixture
async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    jwt_private_key: rsa.RSAPrivateKey,
) -> Seed:
    return await _seed_database(session_factory, jwt_private_key)


@pytest.fixture
async def research_api(
    seed: Seed,
    test_database_url: str,
    jwt_public_key: rsa.RSAPublicKey,
    tmp_path: Path,
) -> ResearchHarness:
    settings = make_settings(
        database_url=test_database_url,
        oidc_issuer=ISSUER,
        oidc_audience=AUDIENCE,
        log_level="WARNING",
        agent_enabled=True,
        agent_provider="deterministic",
        agent_model="integration-test",
        agent_max_rounds=1,
        agent_min_findings_for_followup=1,
        agent_max_task_attempts=1,
        report_enabled=True,
        report_auto_review=True,
        report_max_followup_rounds=0,
        report_review_poll_interval_seconds=0.1,
        report_export_poll_interval_seconds=0.1,
        report_min_independent_sources=1,
        agent_max_seconds=60,
        agent_task_timeout_seconds=30,
        collector_enabled=True,
        object_storage_backend="local",
        object_storage_local_path=str(tmp_path / "research_objects"),
        collector_respect_robots=False,
        collector_domain_min_interval_seconds=0,
        web_search_provider="http",
        web_search_api_url="https://search.example.test/query",
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
        services = application.state.agent_services
        review_services = application.state.review_services
        assert isinstance(services, AgentServices)
        assert isinstance(review_services, ReviewServices)
        assert services.collector_services is not None
        if services.collector_services.search_provider is not None:
            await services.collector_services.search_provider.aclose()
        services.collector_services.search_provider = FakeSearchProvider(
            url="https://example.test/market-report"
        )
        session_factory = application.state.session_factory
        source_id = uuid4()
        snapshot_id = uuid4()
        url = "https://example.test/market-report"
        now = datetime.now(UTC)
        with tenant_context(seed.tenant_a):
            async with session_factory() as session, session.begin():
                source = WebSource(
                    id=source_id,
                    tenant_id=seed.tenant_a,
                    workspace_id=seed.workspace_a,
                    url=url,
                    url_hash=hashlib.sha256(url.encode()).hexdigest(),
                    domain="example.test",
                    title="Market report",
                    status="active",
                    content_sha256=hashlib.sha256(b"report").hexdigest(),
                    latest_snapshot_id=snapshot_id,
                    first_seen_at=now,
                    last_fetched_at=now,
                    created_by=seed.user_ids["workspace_editor_a"],
                )
                session.add(source)
                await session.flush()
                session.add(
                    SourceSnapshot(
                        id=snapshot_id,
                        tenant_id=seed.tenant_a,
                        workspace_id=seed.workspace_a,
                        source_id=source_id,
                        status="active",
                        object_key="test/snapshot",
                        content_type="text/html",
                        size_bytes=6,
                        content_sha256=hashlib.sha256(b"report").hexdigest(),
                        final_url=url,
                        http_status=200,
                        extracted_text=(
                            "Industry demand, competitor positioning, and customer adoption "
                            "all improved. Revenue grew 20 percent in 2025."
                        ),
                        extracted_char_count=120,
                        fetched_at=now,
                    )
                )
        yield ResearchHarness(
            client=client,
            seed=seed,
            app=application,
            services=services,
            review_services=review_services,
            session_factory=session_factory,
            source_id=source_id,
        )


async def _create_run(
    harness: ResearchHarness,
    *,
    user: str = "workspace_editor_a",
    project_id: UUID | None = None,
    request_key: str | None = None,
    objective: str = "Assess the enterprise market opportunity",
) -> dict[str, object]:
    response = await harness.client.post(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs",
        headers=_auth(harness.seed.tokens[user]),
        json={
            "objective": objective,
            "project_id": str(project_id) if project_id else None,
            "request_key": request_key,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


async def _run_worker(harness: ResearchHarness) -> int:
    runner = ResearchRunner(
        services=harness.services,
        session_factory=harness.session_factory,
    )
    return await runner.run_once(tenant_ids=[harness.seed.tenant_a])


async def _run_review_worker(harness: ResearchHarness) -> int:
    runner = ReviewRunner(
        settings=harness.app.state.settings,
        services=harness.review_services,
        session_factory=harness.session_factory,
    )
    return await runner.run_once(tenant_ids=[harness.seed.tenant_a])


async def _run_export_worker(harness: ResearchHarness) -> int:
    runner = ExportRunner(
        settings=harness.app.state.settings,
        services=harness.review_services,
        session_factory=harness.session_factory,
    )
    return await runner.run_once(tenant_ids=[harness.seed.tenant_a])


async def test_research_run_completes_with_source_bound_findings(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    created = await _create_run(harness)

    processed = await _run_worker(harness)
    assert processed == 1

    detail = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}",
        headers=_auth(harness.seed.tokens["workspace_viewer_a"]),
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "completed"
    assert body["model_provider"] == "deterministic"
    assert body["model_name"] == "integration-test"
    assert body["graph_version"] == "stage5-v1"
    assert body["finding_count"] >= 1
    assert body["checkpoint_count"] >= 3
    assert body["usage"]["input_tokens"] > 0
    assert body["usage"]["tool_calls"] >= 1

    findings = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}/findings",
        headers=_auth(harness.seed.tokens["workspace_viewer_a"]),
    )
    assert findings.status_code == 200, findings.text
    finding_rows = findings.json()
    assert finding_rows
    assert finding_rows[0]["source_kind"] == "external_source"
    assert finding_rows[0]["source_id"] == str(harness.source_id)
    assert finding_rows[0]["evidence"]
    assert finding_rows[0]["untrusted"] is True

    calls = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}/tool-calls",
        headers=_auth(harness.seed.tokens["workspace_viewer_a"]),
    )
    assert calls.status_code == 200, calls.text
    tool_names = {call["tool_name"] for call in calls.json()}
    assert tool_names == {"source_search", "web_search"}
    assert tool_names <= {
        "knowledge_search",
        "source_search",
        "source_read",
        "web_search",
    }

    checkpoints = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}/checkpoints",
        headers=_auth(harness.seed.tokens["workspace_viewer_a"]),
    )
    assert checkpoints.status_code == 200, checkpoints.text
    phases = {checkpoint["phase"] for checkpoint in checkpoints.json()}
    assert "finalized" in phases


async def test_research_authorization_and_tenant_isolation(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    forbidden = await harness.client.post(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs",
        headers=_auth(harness.seed.tokens["workspace_viewer_a"]),
        json={"objective": "Viewer should not create a run"},
    )
    assert forbidden.status_code == 403

    created = await _create_run(harness)
    cross_tenant = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}",
        headers=_auth(harness.seed.tokens["tenant_admin_b"]),
    )
    assert cross_tenant.status_code == 404

    project_run = await _create_run(
        harness,
        user="project_editor_a",
        project_id=harness.seed.project_a,
        objective="Project-scoped market research",
    )
    project_editor_read = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{project_run['id']}",
        headers=_auth(harness.seed.tokens["project_editor_a"]),
    )
    assert project_editor_read.status_code == 200
    outsider_read = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{project_run['id']}",
        headers=_auth(harness.seed.tokens["outsider_a"]),
    )
    assert outsider_read.status_code == 403

    workspace_run_read_by_project_only = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}",
        headers=_auth(harness.seed.tokens["project_editor_a"]),
    )
    assert workspace_run_read_by_project_only.status_code == 403
    workspace_list_by_project_only = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs",
        headers=_auth(harness.seed.tokens["project_editor_a"]),
    )
    assert workspace_list_by_project_only.status_code == 403


async def test_pause_resume_and_cancel_flow(research_api: ResearchHarness) -> None:
    harness = research_api
    created = await _create_run(harness)
    base = f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}"

    paused = await harness.client.post(
        f"{base}/pause",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"

    resumed = await harness.client.post(
        f"{base}/resume",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["status"] == "pending"

    await _run_worker(harness)
    completed = await harness.client.get(
        base,
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert completed.json()["status"] == "completed"

    cancel_target = await _create_run(
        harness,
        objective="A run that will be canceled",
    )
    canceled = await harness.client.post(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{cancel_target['id']}/cancel",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert canceled.status_code == 200
    assert canceled.json()["status"] == "canceled"


async def test_idempotency_key_and_run_listing(research_api: ResearchHarness) -> None:
    harness = research_api
    first = await _create_run(harness, request_key="request-0001")
    second = await _create_run(harness, request_key="request-0001")
    assert first["id"] == second["id"]

    conflict = await harness.client.post(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
        json={
            "objective": "A different objective",
            "request_key": "request-0001",
        },
    )
    assert conflict.status_code == 409

    listing = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs",
        headers=_auth(harness.seed.tokens["workspace_viewer_a"]),
    )
    assert listing.status_code == 200
    assert any(run["id"] == first["id"] for run in listing.json())


async def test_pause_while_running_resumes_from_checkpoint(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    created = await _create_run(harness, objective="Pause and resume a live run")
    base = f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}"
    slow = BlockingModel()
    harness.services.model = slow
    runner = ResearchRunner(
        services=harness.services,
        session_factory=harness.session_factory,
    )
    worker = asyncio.create_task(runner.run_once(tenant_ids=[harness.seed.tenant_a]))
    await asyncio.wait_for(slow.started.wait(), timeout=5)

    paused = await harness.client.post(
        f"{base}/pause",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["pause_requested"] is True
    slow.release.set()
    await asyncio.wait_for(worker, timeout=10)

    paused_detail = await harness.client.get(
        base,
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert paused_detail.json()["status"] == "paused"
    paused_checkpoints = paused_detail.json()["checkpoint_count"]

    calls_before = await harness.client.get(
        f"{base}/tool-calls",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert calls_before.status_code == 200
    tool_call_count = len(calls_before.json())

    harness.services.model = DeterministicResearchModel(model="fast-resume")
    resumed = await harness.client.post(
        f"{base}/resume",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert resumed.status_code == 202, resumed.text
    processed = await _run_worker(harness)
    assert processed == 1

    completed = await harness.client.get(
        base,
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["checkpoint_count"] > paused_checkpoints

    calls_after = await harness.client.get(
        f"{base}/tool-calls",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert calls_after.status_code == 200
    assert len(calls_after.json()) == tool_call_count


async def test_authorization_revocation_stops_a_running_research(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    created = await _create_run(harness, objective="Authorization recheck")
    base = f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}"
    slow = BlockingModel()
    harness.services.model = slow
    runner = ResearchRunner(
        services=harness.services,
        session_factory=harness.session_factory,
    )
    worker = asyncio.create_task(runner.run_once(tenant_ids=[harness.seed.tenant_a]))
    await asyncio.wait_for(slow.started.wait(), timeout=5)

    with tenant_context(harness.seed.tenant_a):
        async with harness.session_factory() as session, session.begin():
            membership = await session.scalar(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.tenant_id == harness.seed.tenant_a,
                    WorkspaceMembership.workspace_id == harness.seed.workspace_a,
                    WorkspaceMembership.user_id == harness.seed.user_ids["workspace_editor_a"],
                )
            )
            assert membership is not None
            membership.status = "revoked"

    slow.release.set()
    await asyncio.wait_for(worker, timeout=10)

    detail = await harness.client.get(
        base,
        headers=_auth(harness.seed.tokens["workspace_admin_a"]),
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "authorization_changed"
    assert body["checkpoint_count"] >= 1


async def test_agent_tool_registry_exposes_only_read_tools(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    with tenant_context(harness.seed.tenant_a):
        async with harness.session_factory() as session:
            principal = await resolve_principal_for_user(
                session,
                tenant_id=harness.seed.tenant_a,
                user_id=harness.seed.user_ids["workspace_editor_a"],
            )
    toolbox = ResearchToolbox(
        settings=harness.app.state.settings,
        session_factory=harness.session_factory,
        principal=principal,
        organization_id=harness.seed.org_a,
        workspace_id=harness.seed.workspace_a,
        project_id=None,
        run_id=uuid4(),
        document_scope=None,
        rag_services=None,
        collector_services=harness.services.collector_services,
    )
    assert toolbox.available_tools == {
        ResearchToolName.SOURCE_SEARCH,
        ResearchToolName.SOURCE_READ,
        ResearchToolName.WEB_SEARCH,
    }
    assert all(
        tool
        in {
            ResearchToolName.KNOWLEDGE_SEARCH,
            ResearchToolName.SOURCE_SEARCH,
            ResearchToolName.SOURCE_READ,
            ResearchToolName.WEB_SEARCH,
        }
        for tool in toolbox.available_tools
    )


async def test_budget_exhaustion_is_persisted_as_a_run_status(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    harness.app.state.settings.agent_max_input_tokens = 5
    created = await _create_run(harness, objective="Exhaust a tiny token budget")

    processed = await _run_worker(harness)
    assert processed == 1

    detail = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "budget_exhausted"
    assert body["stop_reason"] == "budget_exhausted"
    assert body["finding_count"] == 0


async def test_worker_does_not_claim_another_tenant_run(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    created_a = await _create_run(harness, objective="Tenant A research")

    response_b = await harness.client.post(
        f"/api/v1/workspaces/{harness.seed.workspace_b}/research-runs",
        headers=_auth(harness.seed.tokens["tenant_admin_b"]),
        json={"objective": "Tenant B research"},
    )
    assert response_b.status_code == 202, response_b.text
    run_b = response_b.json()

    processed = await _run_worker(harness)
    assert processed == 1

    detail_a = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created_a['id']}",
        headers=_auth(harness.seed.tokens["workspace_editor_a"]),
    )
    assert detail_a.json()["status"] == "completed"

    detail_b = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_b}/research-runs/{run_b['id']}",
        headers=_auth(harness.seed.tokens["tenant_admin_b"]),
    )
    assert detail_b.status_code == 200
    assert detail_b.json()["status"] == "pending"


async def test_review_report_approval_and_export(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    created = await _create_run(harness, objective="Reported market opportunity")
    await _run_worker(harness)
    assert await _run_review_worker(harness) == 1

    base = f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}"
    viewer = _auth(harness.seed.tokens["workspace_viewer_a"])
    admin = _auth(harness.seed.tokens["workspace_admin_a"])
    editor = _auth(harness.seed.tokens["workspace_editor_a"])

    cross_tenant = await harness.client.get(
        f"{base}/review",
        headers=_auth(harness.seed.tokens["tenant_admin_b"]),
    )
    assert cross_tenant.status_code == 404

    review = await harness.client.get(f"{base}/review", headers=viewer)
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "completed"
    assert review.json()["claim_count"] >= 1
    assert review.json()["supported_count"] >= 1

    claims = await harness.client.get(f"{base}/review/claims", headers=viewer)
    assert claims.status_code == 200, claims.text
    assert claims.json()
    assert claims.json()[0]["citations"]
    assert claims.json()[0]["citations"][0]["evidence"]["quote"]

    report = await harness.client.get(f"{base}/report", headers=viewer)
    assert report.status_code == 200, report.text
    report_body = report.json()
    assert report_body["status"] == "draft"
    version_id = report_body["current_version_id"]

    versions = await harness.client.get(f"{base}/report/versions", headers=viewer)
    assert versions.status_code == 200
    assert versions.json()[0]["version_number"] == 1

    detail = await harness.client.get(
        f"{base}/report/versions/{version_id}",
        headers=viewer,
    )
    assert detail.status_code == 200, detail.text
    assert "## Evidence Links" in detail.json()["content_markdown"]
    assert detail.json()["structured"]["claims"]

    forbidden_approve = await harness.client.post(
        f"{base}/report/versions/{version_id}/approve",
        headers=viewer,
    )
    assert forbidden_approve.status_code == 403

    approved = await harness.client.post(
        f"{base}/report/versions/{version_id}/approve",
        headers=admin,
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    forbidden_export = await harness.client.post(
        f"{base}/report/versions/{version_id}/exports",
        headers=viewer,
        json={"format": "markdown"},
    )
    assert forbidden_export.status_code == 403

    queued = await harness.client.post(
        f"{base}/report/versions/{version_id}/exports",
        headers=editor,
        json={"format": "markdown"},
    )
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["id"]
    assert queued.json()["status"] == "pending"

    assert await _run_export_worker(harness) == 1
    job = await harness.client.get(f"{base}/report/exports/{job_id}", headers=viewer)
    assert job.status_code == 200
    assert job.json()["status"] == "succeeded"
    assert job.json()["size_bytes"] > 0

    download = await harness.client.get(
        f"{base}/report/exports/{job_id}/download",
        headers=viewer,
    )
    assert download.status_code == 200
    assert download.content.startswith(b"# Research Report")
    assert "attachment" in download.headers["content-disposition"]

    audit = await harness.client.get(
        "/api/v1/audit-events",
        headers=_auth(harness.seed.tokens["tenant_admin_a"]),
        params={"limit": 200},
    )
    assert audit.status_code == 200
    actions = {event["action"] for event in audit.json()}
    assert "report.version_approved" in actions
    assert "report.export_succeeded" in actions


async def test_report_versioning_after_manual_rereview(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    created = await _create_run(harness, objective="Versioned report")
    await _run_worker(harness)
    assert await _run_review_worker(harness) == 1
    base = f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}"
    admin = _auth(harness.seed.tokens["workspace_admin_a"])
    editor = _auth(harness.seed.tokens["workspace_editor_a"])
    report = (await harness.client.get(f"{base}/report", headers=admin)).json()
    first_version = report["current_version_id"]
    approved = await harness.client.post(
        f"{base}/report/versions/{first_version}/approve",
        headers=admin,
    )
    assert approved.status_code == 200

    queued = await harness.client.post(f"{base}/review", headers=editor)
    assert queued.status_code == 202, queued.text
    await _run_review_worker(harness)

    report_after = await harness.client.get(f"{base}/report", headers=admin)
    assert report_after.json()["status"] == "draft"
    assert report_after.json()["current_version_id"] != first_version
    versions = await harness.client.get(f"{base}/report/versions", headers=admin)
    assert [item["version_number"] for item in versions.json()] == [2, 1]
    assert versions.json()[0]["status"] == "draft"
    assert versions.json()[1]["status"] == "approved"

    rejected = await harness.client.post(
        f"{base}/report/versions/{report_after.json()['current_version_id']}/reject",
        headers=admin,
        json={"reason": "Needs stronger independent corroboration"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["rejection_reason"] == "Needs stronger independent corroboration"
    report_rejected = await harness.client.get(f"{base}/report", headers=admin)
    assert report_rejected.json()["status"] == "rejected"


async def test_review_creates_bounded_followup_and_downgrades_gaps(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    created = await _create_run(harness, objective="Follow-up evidence test")
    await _run_worker(harness)
    base = f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{created['id']}"

    deleted = await harness.client.delete(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/sources/{harness.source_id}",
        headers=_auth(harness.seed.tokens["workspace_admin_a"]),
    )
    assert deleted.status_code == 204, deleted.text

    harness.app.state.settings.report_max_followup_rounds = 1
    editor = _auth(harness.seed.tokens["workspace_editor_a"])
    assert await _run_review_worker(harness) == 1
    awaiting = await harness.client.get(f"{base}/review", headers=editor)
    assert awaiting.status_code == 200, awaiting.text
    assert awaiting.json()["status"] == "awaiting_followup"
    followup_run_id = awaiting.json()["followup_run_id"]
    assert followup_run_id is not None
    assert awaiting.json()["followup_round"] == 1

    assert await _run_worker(harness) == 1
    followup_run = await harness.client.get(
        f"/api/v1/workspaces/{harness.seed.workspace_a}/research-runs/{followup_run_id}",
        headers=editor,
    )
    assert followup_run.status_code == 200
    assert followup_run.json()["run_kind"] == "followup"
    assert followup_run.json()["root_run_id"] == created["id"]
    assert followup_run.json()["status"] == "failed"

    assert await _run_review_worker(harness) == 1
    completed = await harness.client.get(f"{base}/review", headers=editor)
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["followup_round"] == 1
    assert completed.json()["invalid_count"] >= 1

    report = await harness.client.get(f"{base}/report", headers=editor)
    assert report.status_code == 200
    version_id = report.json()["current_version_id"]
    version = await harness.client.get(
        f"{base}/report/versions/{version_id}",
        headers=editor,
    )
    assert version.status_code == 200
    body = version.json()
    assert body["structured"]["followup_round"] == 1
    assert "Insufficient evidence" in body["content_markdown"]
    assert "Conflicts and Gaps" in body["content_markdown"]


async def test_event_admin_api_lists_and_replays_failed_events(
    research_api: ResearchHarness,
) -> None:
    harness = research_api
    await _create_run(harness, objective="Event API test")
    admin = _auth(harness.seed.tokens["tenant_admin_a"])
    viewer = _auth(harness.seed.tokens["workspace_viewer_a"])

    forbidden = await harness.client.get("/api/v1/events", headers=viewer)
    assert forbidden.status_code == 403

    listing = await harness.client.get(
        "/api/v1/events",
        headers=admin,
        params={"event_type": "research_run.created"},
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()
    event_id = listing.json()[0]["id"]

    with tenant_context(harness.seed.tenant_a):
        async with harness.session_factory() as session, session.begin():
            event = await session.scalar(
                select(DomainEvent).where(DomainEvent.id == UUID(event_id))
            )
            assert event is not None
            event.status = "failed"
            event.last_error = "forced"

    replayed = await harness.client.post(
        f"/api/v1/events/{event_id}/retry",
        headers=admin,
    )
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["status"] == "pending"

    failures = await harness.client.get(
        "/api/v1/events/consumer-failures",
        headers=admin,
    )
    assert failures.status_code == 200

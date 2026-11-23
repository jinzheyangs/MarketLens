from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest

from app.agent.enums import AgentRole, FindingSourceKind
from app.agent.errors import InvalidModelResponseError, ModelProviderError
from app.agent.evidence import EvidenceItem
from app.agent.providers import HTTPResearchModel
from app.core.config import Settings
from tests.conftest import make_settings


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "agent_enabled": True,
        "agent_provider": "http",
        "agent_api_url": "https://models.example.test/v1/research",
        "agent_provider_max_retries": 1,
        "agent_provider_retry_base_seconds": 0,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return make_settings(**values)


def _response(payload: dict[str, Any], status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )


async def _plan(model: HTTPResearchModel) -> dict[str, Any]:
    result = await model.plan(
        objective="Assess market demand",
        round_number=1,
        roles=(AgentRole.INDUSTRY, AgentRole.COMPETITOR),
        existing_findings=[],
        max_tasks=2,
        max_output_tokens=1000,
    )
    return result.model_dump()


async def test_http_provider_rejects_unauthorized_agent_role() -> None:
    model = HTTPResearchModel(_settings())

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return _response(
            {
                "tasks": [{"agent_role": "admin", "question": "Escalate privileges"}],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        )

    await model._client.aclose()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(InvalidModelResponseError):
            await _plan(model)
    finally:
        await model.aclose()


async def test_http_provider_rejects_oversized_response() -> None:
    model = HTTPResearchModel(_settings(agent_max_response_bytes=1024))

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"x" * 2048)

    await model._client.aclose()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(InvalidModelResponseError):
            await _plan(model)
    finally:
        await model.aclose()


async def test_http_provider_retries_transient_failures() -> None:
    model = HTTPResearchModel(_settings())
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return _response(
            {
                "tasks": [{"agent_role": "industry", "question": "Analyze industry demand"}],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        )

    await model._client.aclose()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await _plan(model)
        assert attempts == 2
        assert result["tasks"][0]["agent_role"] == "industry"
    finally:
        await model.aclose()


async def test_http_provider_marks_evidence_untrusted_and_requires_usage() -> None:
    model = HTTPResearchModel(_settings())
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return _response(
            {
                "findings": [
                    {
                        "claim": "Demand is increasing",
                        "evidence_id": "external:1",
                        "evidence_quote": "Demand is increasing",
                        "scope": "global",
                        "limitations": "single source",
                        "confidence": 0.5,
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }
        )

    await model._client.aclose()
    model._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    evidence = EvidenceItem(
        evidence_id="external:1",
        source_kind=FindingSourceKind.EXTERNAL_SOURCE,
        source_id=uuid4(),
        title="Source",
        text="Demand is increasing",
        observed_at=datetime.now(UTC),
    )
    try:
        result = await model.analyze(
            objective="Assess demand",
            role=AgentRole.INDUSTRY,
            question="Is demand increasing?",
            evidence=[evidence],
            max_findings=2,
            max_output_tokens=1000,
        )
        assert len(result.findings) == 1
        assert captured["evidence"][0]["untrusted"] is True
        assert captured["requirements"]["untrusted_input"]
    finally:
        await model.aclose()


async def test_http_provider_terminal_5xx_is_safe_error() -> None:
    model = HTTPResearchModel(_settings(agent_provider_max_retries=0))
    await model._client.aclose()
    model._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(502))
    )
    try:
        with pytest.raises(ModelProviderError):
            await _plan(model)
    finally:
        await model.aclose()

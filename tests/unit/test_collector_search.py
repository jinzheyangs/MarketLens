from __future__ import annotations

import json

import httpx
import pytest

from app.collector.errors import SearchProviderError
from app.collector.search import HttpSearchProvider, SearchHit, build_search_provider
from app.collector.service import filter_search_hits
from tests.conftest import make_settings


async def test_http_search_provider_parses_and_sends_headers() -> None:
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["x-api-key"] == "secret"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.com/a", "title": "A", "snippet": "one"},
                    {"url": "https://example.com/b", "title": "B", "snippet": "two"},
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HttpSearchProvider(
        api_url="https://search.test/query",
        api_key="secret",
        api_key_header="X-API-Key",
        timeout_seconds=5,
        max_results=10,
        client=client,
    )
    try:
        hits = await provider.search("market", limit=1)
    finally:
        await provider.aclose()
        await client.aclose()
    assert hits == [SearchHit(url="https://example.com/a", title="A", snippet="one")]
    assert seen == [{"query": "market", "limit": 1}]


async def test_http_search_provider_rejects_invalid_or_failed_payloads() -> None:
    async def invalid(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not_results": []})

    async def failing(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    for handler in (invalid, failing):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        provider = HttpSearchProvider(
            api_url="https://search.test/query",
            api_key=None,
            api_key_header="X-API-Key",
            timeout_seconds=5,
            max_results=10,
            client=client,
        )
        try:
            with pytest.raises(SearchProviderError):
                await provider.search("query", limit=1)
        finally:
            await provider.aclose()
            await client.aclose()


def test_search_provider_configuration_and_hit_filtering() -> None:
    assert build_search_provider(make_settings(web_search_provider="none")) is None

    settings = make_settings(
        web_search_provider="http",
        web_search_api_url="https://search.test/query",
        collector_blocked_domains=["blocked.example"],
        collector_allowed_ports=[80, 443],
    )
    hits = [
        SearchHit(url="https://example.com/a", title="A", snippet="one"),
        SearchHit(url="https://blocked.example/", title="Blocked", snippet=None),
        SearchHit(url="http://127.0.0.1/", title="Private", snippet=None),
        SearchHit(url="https://example.com/a#fragment", title="Duplicate", snippet=None),
    ]
    filtered = filter_search_hits(hits, settings=settings, limit=10)
    assert [hit.url for hit in filtered] == ["https://example.com/a"]

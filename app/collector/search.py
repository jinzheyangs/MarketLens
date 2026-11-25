"""可配置搜索供应商抽象。

The HTTP provider targets a simple, documented JSON contract and is not tied to
a specific vendor. Provider output is untrusted and must still pass URL/domain
policy validation before collection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.collector.errors import SearchProviderError
from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str | None
    snippet: str | None


class SearchProvider(Protocol):
    async def search(self, query: str, *, limit: int) -> list[SearchHit]: ...

    async def aclose(self) -> None: ...


class HttpSearchProvider:
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str | None,
        api_key_header: str,
        timeout_seconds: float,
        max_results: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_url = api_url
        self._api_key = api_key
        self._api_key_header = api_key_header
        self._max_results = max_results
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    async def search(self, query: str, *, limit: int) -> list[SearchHit]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers[self._api_key_header] = self._api_key
        try:
            response = await self._client.post(
                self._api_url,
                json={"query": query, "limit": min(limit, self._max_results)},
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchProviderError("search provider request failed") from exc

        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise SearchProviderError("search provider returned an invalid payload")
        hits: list[SearchHit] = []
        for item in body["results"][: min(limit, self._max_results)]:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            title = item.get("title")
            snippet = item.get("snippet")
            hits.append(
                SearchHit(
                    url=url.strip()[:2048],
                    title=str(title)[:500] if title is not None else None,
                    snippet=str(snippet)[:1000] if snippet is not None else None,
                )
            )
        return hits

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def build_search_provider(settings: Settings) -> SearchProvider | None:
    if settings.web_search_provider == "none":
        return None
    if not settings.web_search_api_url:
        raise SearchProviderError("WEB_SEARCH_API_URL is not configured")
    return HttpSearchProvider(
        api_url=settings.web_search_api_url,
        api_key=(
            settings.web_search_api_key.get_secret_value() if settings.web_search_api_key else None
        ),
        api_key_header=settings.web_search_api_key_header,
        timeout_seconds=settings.web_search_timeout_seconds,
        max_results=settings.web_search_max_results,
    )


__all__ = ["HttpSearchProvider", "SearchHit", "SearchProvider", "build_search_provider"]


# Keep the Any import available for provider extensions that parse richer payloads.
_ = Any

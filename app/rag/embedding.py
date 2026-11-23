"""Embedding provider abstraction.

The hash provider is deterministic and exists for development/tests only.
Production validation refuses it and requires an HTTP embedding endpoint.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol

import httpx

from app.core.config import Settings
from app.rag.errors import EmbeddingError

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+|[㐀-鿿]")


class EmbeddingProvider(Protocol):
    dimension: int
    model: str
    version: str

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def aclose(self) -> None: ...


class HashEmbeddingProvider:
    """Signed feature hashing over lexical tokens (development only)."""

    def __init__(self, *, dimension: int, model: str, version: str) -> None:
        self.dimension = dimension
        self.model = model
        self.version = version

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    async def aclose(self) -> None:
        return None

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in _TOKEN_PATTERN.findall(text.lower()):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            number = int.from_bytes(digest, "big")
            index = number % self.dimension
            sign = 1.0 if (number >> 63) & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


class HTTPEmbeddingProvider:
    """OpenAI-compatible embeddings endpoint with bounded retries."""

    def __init__(
        self,
        *,
        dimension: int,
        model: str,
        version: str,
        api_url: str,
        api_key: str | None,
        timeout_seconds: float,
        batch_size: int,
        max_retries: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.dimension = dimension
        self.model = model
        self.version = version
        self._api_url = api_url
        self._api_key = api_key
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            trust_env=False,
        )

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        embeddings: list[list[float]] = []
        for offset in range(0, len(texts), self._batch_size):
            embeddings.extend(
                await self._embed_batch(list(texts[offset : offset + self._batch_size]))
            )
        return embeddings

    async def embed_query(self, text: str) -> list[float]:
        embeddings = await self.embed_texts([text])
        if not embeddings:
            raise EmbeddingError("embedding endpoint returned no vector")
        return embeddings[0]

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": batch}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(self._api_url, json=payload, headers=headers)
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_error = EmbeddingError(
                        f"embedding endpoint returned {response.status_code}"
                    )
                    if attempt < self._max_retries:
                        await asyncio.sleep(min(2**attempt * 0.25, 2.0))
                        continue
                    raise last_error
                response.raise_for_status()
                return self._parse_response(response.json(), expected=len(batch))
            except EmbeddingError:
                if attempt >= self._max_retries:
                    raise
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt < self._max_retries:
                    await asyncio.sleep(min(2**attempt * 0.25, 2.0))
                    continue
                raise EmbeddingError("embedding request failed") from exc

        raise EmbeddingError("embedding request failed") from last_error

    def _parse_response(self, body: object, *, expected: int) -> list[list[float]]:
        if not isinstance(body, dict):
            raise EmbeddingError("embedding response is not an object")
        items = body.get("data")
        if not isinstance(items, list) or len(items) != expected:
            raise EmbeddingError("embedding response has the wrong item count")

        if all(isinstance(item, dict) and isinstance(item.get("index"), int) for item in items):
            items = sorted(items, key=lambda item: item["index"])
            if [item["index"] for item in items] != list(range(expected)):
                raise EmbeddingError("embedding response indices are invalid")

        embeddings: list[list[float]] = []
        for item in items:
            if not isinstance(item, dict):
                raise EmbeddingError("embedding response item is invalid")
            vector = item.get("embedding")
            if not isinstance(vector, list) or len(vector) != self.dimension:
                raise EmbeddingError("embedding response has the wrong dimension")
            normalized: list[float] = []
            for value in vector:
                if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise EmbeddingError("embedding contains a non-finite value")
                normalized.append(float(value))
            embeddings.append(normalized)
        return embeddings


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "hash":
        return HashEmbeddingProvider(
            dimension=settings.embedding_dimension,
            model=settings.embedding_model,
            version=settings.embedding_version,
        )
    if not settings.embedding_api_url:
        raise EmbeddingError("EMBEDDING_API_URL is required")
    return HTTPEmbeddingProvider(
        dimension=settings.embedding_dimension,
        model=settings.embedding_model,
        version=settings.embedding_version,
        api_url=settings.embedding_api_url,
        api_key=(
            settings.embedding_api_key.get_secret_value() if settings.embedding_api_key else None
        ),
        timeout_seconds=settings.embedding_timeout_seconds,
        batch_size=settings.embedding_batch_size,
        max_retries=settings.embedding_max_retries,
    )

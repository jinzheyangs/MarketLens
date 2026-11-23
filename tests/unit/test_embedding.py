from __future__ import annotations

import json
import math

import httpx
import pytest

from app.rag.embedding import HashEmbeddingProvider, HTTPEmbeddingProvider
from app.rag.errors import EmbeddingError


async def test_hash_embedding_is_deterministic_and_normalized() -> None:
    provider = HashEmbeddingProvider(dimension=8, model="hash-v1", version="1")
    first = await provider.embed_texts(["alpha beta", "alpha beta"])
    second = await provider.embed_query("alpha beta")
    assert first[0] == first[1] == second
    assert len(first[0]) == 8
    assert math.isclose(sum(value * value for value in first[0]), 1.0, rel_tol=1e-6)

    other = await provider.embed_query("gamma delta")
    assert other != first[0]
    empty = await provider.embed_query("!!!")
    assert empty == [0.0] * 8
    await provider.aclose()


async def test_http_embedding_parses_valid_response() -> None:
    requests: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["authorization"] == "Bearer secret"
        data = [
            {
                "index": index,
                "embedding": [float(index + 1), 0.0, 0.0, 0.0],
            }
            for index, _ in enumerate(body["input"])
        ]
        return httpx.Response(200, json={"data": list(reversed(data))})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HTTPEmbeddingProvider(
        dimension=4,
        model="embed-v1",
        version="1",
        api_url="https://embeddings.test/v1/embeddings",
        api_key="secret",
        timeout_seconds=5,
        batch_size=2,
        max_retries=0,
        client=client,
    )
    try:
        vectors = await provider.embed_texts(["one", "two", "three"])
    finally:
        await provider.aclose()
        await client.aclose()
    assert len(vectors) == 3
    assert vectors[0] == [1.0, 0.0, 0.0, 0.0]
    assert len(requests) == 2


async def test_http_embedding_rejects_wrong_dimension() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 2.0]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HTTPEmbeddingProvider(
        dimension=4,
        model="embed-v1",
        version="1",
        api_url="https://embeddings.test/v1/embeddings",
        api_key=None,
        timeout_seconds=5,
        batch_size=4,
        max_retries=0,
        client=client,
    )
    try:
        with pytest.raises(EmbeddingError):
            await provider.embed_texts(["one"])
    finally:
        await provider.aclose()
        await client.aclose()


async def test_http_embedding_retries_transient_errors() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"embedding": [1.0, 0.0, 0.0, 0.0]}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HTTPEmbeddingProvider(
        dimension=4,
        model="embed-v1",
        version="1",
        api_url="https://embeddings.test/v1/embeddings",
        api_key=None,
        timeout_seconds=5,
        batch_size=4,
        max_retries=1,
        client=client,
    )
    try:
        vectors = await provider.embed_texts(["one"])
    finally:
        await provider.aclose()
        await client.aclose()
    assert calls == 2
    assert vectors == [[1.0, 0.0, 0.0, 0.0]]

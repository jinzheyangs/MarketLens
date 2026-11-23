from __future__ import annotations

from pathlib import Path

import pytest

from app.rag.errors import ObjectStorageError
from app.rag.storage import LocalObjectStorage


async def test_local_storage_round_trip(tmp_path: Path) -> None:
    storage = LocalObjectStorage(str(tmp_path / "objects"))
    await storage.ensure_ready()
    await storage.health()

    key = "tenants/a/workspaces/b/documents/c/versions/d/original"
    await storage.put(key, b"payload", "text/plain")
    assert await storage.get(key) == b"payload"
    await storage.delete(key)
    with pytest.raises(ObjectStorageError):
        await storage.get(key)
    await storage.delete(key)
    await storage.aclose()


@pytest.mark.parametrize(
    "key",
    [
        "../escape",
        "/absolute",
        "a/../../escape",
        "a\\..\\escape",
    ],
)
async def test_local_storage_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    storage = LocalObjectStorage(str(tmp_path / "objects"))
    await storage.ensure_ready()
    with pytest.raises(ObjectStorageError):
        await storage.put(key, b"x", "text/plain")

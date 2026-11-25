"""对象存储抽象：本地文件系统与 S3 兼容后端。

Only server-generated object keys are used. Client filenames are display metadata,
never path components.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.rag.errors import ObjectStorageError


class ObjectStorage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def health(self) -> None: ...

    async def ensure_ready(self) -> None: ...

    async def aclose(self) -> None: ...


class LocalObjectStorage:
    def __init__(self, root: str) -> None:
        self._root = Path(root).expanduser().resolve()

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)

    async def health(self) -> None:
        if not await asyncio.to_thread(self._root.is_dir):
            raise ObjectStorageError("local object storage directory is missing")

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        del content_type
        path = self._safe_path(key)
        await asyncio.to_thread(self._write_atomic, path, data)

    async def get(self, key: str) -> bytes:
        path = self._safe_path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise ObjectStorageError("object could not be read") from exc

    async def delete(self, key: str) -> None:
        path = self._safe_path(key)
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError as exc:
            raise ObjectStorageError("object could not be deleted") from exc

    async def aclose(self) -> None:
        return None

    def _safe_path(self, key: str) -> Path:
        if not key or key.startswith("/") or "\\" in key:
            raise ObjectStorageError("invalid object key")
        parts = Path(key).parts
        if any(part in {"", ".", ".."} for part in parts):
            raise ObjectStorageError("invalid object key")
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise ObjectStorageError("object key escapes the storage root")
        return path

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temporary_name, path)
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise


class S3ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        if not settings.s3_bucket:
            raise ObjectStorageError("S3_BUCKET is not configured")
        self._bucket = settings.s3_bucket
        self._auto_create = settings.s3_auto_create_bucket
        self._region = settings.s3_region
        access_key = settings.s3_access_key.get_secret_value() if settings.s3_access_key else None
        secret_key = settings.s3_secret_key.get_secret_value() if settings.s3_secret_key else None
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            use_ssl=settings.s3_use_ssl,
            config=BotoConfig(
                signature_version="s3v4",
                connect_timeout=settings.s3_timeout_seconds,
                read_timeout=settings.s3_timeout_seconds,
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": settings.s3_addressing_style},
            ),
        )

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._ensure_ready_sync)

    def _ensure_ready_sync(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 404 or not self._auto_create:
                raise ObjectStorageError("S3 bucket is not accessible") from exc

        try:
            kwargs: dict[str, object] = {"Bucket": self._bucket}
            if self._region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self._region}
            self._client.create_bucket(**kwargs)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError("S3 bucket could not be created") from exc

    async def health(self) -> None:
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self._bucket)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError("S3 bucket is not accessible") from exc

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError("object could not be stored") from exc

    async def get(self, key: str) -> bytes:
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self._bucket,
                Key=key,
            )
            return await asyncio.to_thread(response["Body"].read)
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError("object could not be read") from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=key,
            )
        except (BotoCoreError, ClientError) as exc:
            raise ObjectStorageError("object could not be deleted") from exc

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)


def build_object_storage(settings: Settings) -> ObjectStorage:
    if settings.object_storage_backend == "local":
        return LocalObjectStorage(settings.object_storage_local_path)
    return S3ObjectStorage(settings)


__all__: Sequence[str] = [
    "LocalObjectStorage",
    "ObjectStorage",
    "S3ObjectStorage",
    "build_object_storage",
]

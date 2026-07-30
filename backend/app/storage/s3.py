"""S3 / MinIO storage adapter.

One adapter serves both: MinIO is S3-compatible, so the only differences are the
endpoint URL and path-style addressing (``S3_USE_PATH_STYLE``), both configuration.

``aioboto3`` is imported lazily inside the client factory so a deployment that
uses Azure never needs the dependency installed.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.core.config import get_settings
from app.core.errors import NotFoundError, StorageError
from app.core.logging import get_logger
from app.storage.base import (
    IObjectStorage,
    ObjectMetadata,
    SupportsAsyncRead,
    UploadResult,
)

logger = get_logger(__name__)

#: Multipart threshold. Below this, a single PUT is cheaper than negotiating parts.
_MULTIPART_THRESHOLD = 8 * 1024 * 1024


class S3Storage(IObjectStorage):
    provider = "s3"

    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings.storage
        self.default_container = self.settings.container
        self._session: Any = None

    def _get_session(self) -> Any:
        if self._session is None:
            try:
                import aioboto3
            except ImportError as exc:  # pragma: no cover
                raise StorageError(
                    "S3 storage requires the 's3' extra: pip install '.[s3]'"
                ) from exc
            self._session = aioboto3.Session()
        return self._session

    @asynccontextmanager
    async def _client(self, *, for_signing: bool = False) -> AsyncIterator[Any]:
        """Yield an S3 client.

        A new client per operation is deliberate: aioboto3 clients are bound to the
        event loop and are not safe to share across the many concurrent tasks a
        worker pool runs. The underlying HTTP connections are pooled by botocore.
        """
        from botocore.config import Config

        session = self._get_session()
        config = Config(
            s3={"addressing_style": "path" if self.settings.s3_use_path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        )
        kwargs: dict[str, Any] = {
            "service_name": "s3",
            "region_name": self.settings.s3_region,
            "config": config,
        }
        # Presigning is pure local computation - no request is made - so signing
        # against the browser-facing endpoint is safe and is the only way to get a
        # URL that both resolves for the client and validates at the storage
        # service.
        endpoint = (
            self.settings.s3_signing_endpoint if for_signing else self.settings.s3_endpoint_url
        )
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if self.settings.s3_access_key_id:
            kwargs["aws_access_key_id"] = self.settings.s3_access_key_id
            kwargs["aws_secret_access_key"] = self.settings.s3_secret_access_key

        async with session.client(**kwargs) as client:
            yield client

    def _bucket(self, container: str | None) -> str:
        return container or self.default_container

    # ------------------------------------------------------------------ write
    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        container: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        checksum = hashlib.sha256(data).hexdigest()
        try:
            async with self._client() as client:
                response = await client.put_object(
                    Bucket=self._bucket(container),
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                    # Round-trips the content hash so an integrity check never has
                    # to re-download the object.
                    Metadata={**(metadata or {}), "sha256": checksum},
                )
        except Exception as exc:
            raise StorageError(f"S3 put failed for {key}: {exc}") from exc

        return UploadResult(
            key=key,
            size=len(data),
            etag=str(response.get("ETag", "")).strip('"') or None,
            checksum=checksum,
            container=self._bucket(container),
        )

    async def put_stream(
        self,
        key: str,
        stream: SupportsAsyncRead,
        *,
        content_type: str = "application/octet-stream",
        container: str | None = None,
        length: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        """Upload a stream, switching to multipart once it exceeds the threshold.

        Small objects take one PUT; large ones stream part by part so peak memory
        stays at one part regardless of file size.
        """
        bucket = self._bucket(container)
        digest = hashlib.sha256()

        # Read one part's worth. If a second read comes back empty the object fits
        # in a single PUT, which avoids the multipart round trips entirely.
        first = await _read_exactly(stream, _MULTIPART_THRESHOLD)
        digest.update(first)

        second = await _read_exactly(stream, _MULTIPART_THRESHOLD)
        if not second:
            return await self.put_bytes(
                key, first, content_type=content_type, container=container, metadata=metadata
            )

        digest.update(second)
        total = len(first) + len(second)
        parts: list[dict[str, Any]] = []
        upload_id: str | None = None

        try:
            async with self._client() as client:
                created = await client.create_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    ContentType=content_type,
                    Metadata=metadata or {},
                )
                upload_id = created["UploadId"]

                # Every part except the last must be at least 5 MiB; the 8 MiB
                # threshold satisfies that by construction.
                pending = first
                part_number = 1
                next_part: bytes | None = second

                while pending:
                    uploaded = await client.upload_part(
                        Bucket=bucket,
                        Key=key,
                        PartNumber=part_number,
                        UploadId=upload_id,
                        Body=pending,
                    )
                    parts.append({"ETag": uploaded["ETag"], "PartNumber": part_number})
                    part_number += 1

                    pending = next_part or b""
                    if pending:
                        chunk = await _read_exactly(stream, _MULTIPART_THRESHOLD)
                        if chunk:
                            digest.update(chunk)
                            total += len(chunk)
                        next_part = chunk or None

                completed = await client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
        except Exception as exc:
            # An abandoned multipart upload is billable storage that never appears
            # in a listing, so always abort on failure.
            if upload_id:
                try:
                    async with self._client() as client:
                        await client.abort_multipart_upload(
                            Bucket=bucket, Key=key, UploadId=upload_id
                        )
                except Exception:  # noqa: BLE001
                    logger.error("multipart_abort_failed", key=key, upload_id=upload_id)
            raise StorageError(f"S3 stream upload failed for {key}: {exc}") from exc

        return UploadResult(
            key=key,
            size=total,
            etag=str(completed.get("ETag", "")).strip('"') or None,
            checksum=digest.hexdigest(),
            container=bucket,
        )

    # ------------------------------------------------------------------- read
    async def get_bytes(self, key: str, *, container: str | None = None) -> bytes:
        try:
            async with self._client() as client:
                response = await client.get_object(Bucket=self._bucket(container), Key=key)
                async with response["Body"] as body:
                    return bytes(await body.read())
        except Exception as exc:
            if _is_missing(exc):
                raise NotFoundError("Object", key) from exc
            raise StorageError(f"S3 get failed for {key}: {exc}") from exc

    async def get_stream(
        self, key: str, *, container: str | None = None, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        try:
            async with self._client() as client:
                response = await client.get_object(Bucket=self._bucket(container), Key=key)
                async with response["Body"] as body:
                    while True:
                        chunk = await body.read(chunk_size)
                        if not chunk:
                            break
                        yield chunk
        except Exception as exc:
            if _is_missing(exc):
                raise NotFoundError("Object", key) from exc
            raise StorageError(f"S3 stream failed for {key}: {exc}") from exc

    async def head(self, key: str, *, container: str | None = None) -> ObjectMetadata | None:
        try:
            async with self._client() as client:
                response = await client.head_object(Bucket=self._bucket(container), Key=key)
        except Exception as exc:
            if _is_missing(exc):
                return None
            raise StorageError(f"S3 head failed for {key}: {exc}") from exc

        return ObjectMetadata(
            key=key,
            size=int(response.get("ContentLength", 0)),
            content_type=str(response.get("ContentType", "application/octet-stream")),
            etag=str(response.get("ETag", "")).strip('"') or None,
            last_modified=response.get("LastModified"),
            checksum=(response.get("Metadata") or {}).get("sha256"),
            metadata=response.get("Metadata"),
        )

    async def exists(self, key: str, *, container: str | None = None) -> bool:
        return await self.head(key, container=container) is not None

    # ----------------------------------------------------------------- delete
    async def delete(self, key: str, *, container: str | None = None) -> bool:
        try:
            async with self._client() as client:
                await client.delete_object(Bucket=self._bucket(container), Key=key)
            return True
        except Exception as exc:
            if _is_missing(exc):
                return False
            raise StorageError(f"S3 delete failed for {key}: {exc}") from exc

    async def delete_prefix(self, prefix: str, *, container: str | None = None) -> int:
        """Delete a prefix in batches of 1000 - the S3 bulk-delete maximum."""
        bucket = self._bucket(container)
        deleted = 0
        try:
            async with self._client() as client:
                paginator = client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                    objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
                    for start in range(0, len(objects), 1000):
                        batch = objects[start : start + 1000]
                        if not batch:
                            continue
                        await client.delete_objects(
                            Bucket=bucket, Delete={"Objects": batch, "Quiet": True}
                        )
                        deleted += len(batch)
        except Exception as exc:
            raise StorageError(f"S3 prefix delete failed for {prefix}: {exc}") from exc
        return deleted

    async def list_keys(
        self, prefix: str, *, container: str | None = None, limit: int = 1000
    ) -> list[str]:
        keys: list[str] = []
        try:
            async with self._client() as client:
                paginator = client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(Bucket=self._bucket(container), Prefix=prefix):
                    for item in page.get("Contents", []):
                        keys.append(str(item["Key"]))
                        if len(keys) >= limit:
                            return keys
        except Exception as exc:
            raise StorageError(f"S3 list failed for {prefix}: {exc}") from exc
        return keys

    # ------------------------------------------------------------------ share
    async def signed_url(
        self,
        key: str,
        *,
        container: str | None = None,
        expires_in: int | None = None,
        download_filename: str | None = None,
    ) -> str:
        ttl = expires_in or self.settings.signed_url_ttl_seconds
        params: dict[str, Any] = {"Bucket": self._bucket(container), "Key": key}
        if download_filename:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_filename}"'
        try:
            async with self._client(for_signing=True) as client:
                return str(
                    await client.generate_presigned_url("get_object", Params=params, ExpiresIn=ttl)
                )
        except Exception as exc:
            raise StorageError(f"Could not sign URL for {key}: {exc}") from exc

    async def copy(
        self,
        source_key: str,
        dest_key: str,
        *,
        source_container: str | None = None,
        dest_container: str | None = None,
    ) -> UploadResult:
        try:
            async with self._client() as client:
                response = await client.copy_object(
                    Bucket=self._bucket(dest_container),
                    Key=dest_key,
                    CopySource={"Bucket": self._bucket(source_container), "Key": source_key},
                )
        except Exception as exc:
            raise StorageError(f"S3 copy failed {source_key} -> {dest_key}: {exc}") from exc

        meta = await self.head(dest_key, container=dest_container)
        return UploadResult(
            key=dest_key,
            size=meta.size if meta else 0,
            etag=str((response.get("CopyObjectResult") or {}).get("ETag", "")).strip('"') or None,
            container=self._bucket(dest_container),
        )

    # ----------------------------------------------------------------- health
    async def health(self) -> bool:
        try:
            async with self._client() as client:
                await client.head_bucket(Bucket=self.default_container)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("s3_storage_unhealthy", error=str(exc))
            return False


async def _read_exactly(stream: SupportsAsyncRead, size: int) -> bytes:
    """Read up to ``size`` bytes, looping until filled or the stream ends.

    A single ``read(size)`` is allowed to return fewer bytes than asked for; a
    multipart part that is short because of that would be rejected as under the
    5 MiB minimum, so the loop is load-bearing rather than defensive.
    """
    buffer = bytearray()
    while len(buffer) < size:
        chunk = await stream.read(size - len(buffer))
        if not chunk:
            break
        buffer.extend(chunk)
    return bytes(buffer)


def _is_missing(exc: Exception) -> bool:
    """Is this exception a 404 rather than a real failure?"""
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    status_code = getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NotFound", "NoSuchBucket"} or status_code == 404


__all__ = ["S3Storage"]

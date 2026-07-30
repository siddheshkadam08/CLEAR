"""Azure Blob Storage adapter - the default production target.

Authentication order: connection string, then account key, then
``DefaultAzureCredential`` (managed identity). Managed identity is preferred in
Azure because it removes the stored secret entirely, so it is tried last as the
fallback that needs no configuration at all.

``azure-storage-blob`` is imported lazily so an S3 deployment does not need it.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
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


class AzureBlobStorage(IObjectStorage):
    provider = "azure"

    def __init__(self) -> None:
        settings = get_settings()
        self.settings = settings.storage
        self.default_container = self.settings.container
        self._credential: Any = None

    def _service_client(self) -> Any:
        try:
            from azure.storage.blob.aio import BlobServiceClient
        except ImportError as exc:  # pragma: no cover
            raise StorageError(
                "Azure storage requires the 'azure' extra: pip install '.[azure]'"
            ) from exc

        if self.settings.azure_connection_string:
            return BlobServiceClient.from_connection_string(self.settings.azure_connection_string)

        account = self.settings.azure_account_name
        if not account:
            raise StorageError(
                "Azure storage requires AZURE_STORAGE_CONNECTION_STRING or "
                "AZURE_STORAGE_ACCOUNT_NAME."
            )
        url = f"https://{account}.blob.core.windows.net"

        if self.settings.azure_account_key:
            return BlobServiceClient(account_url=url, credential=self.settings.azure_account_key)

        # No stored secret: use the pod/VM managed identity.
        from azure.identity.aio import DefaultAzureCredential

        if self._credential is None:
            self._credential = DefaultAzureCredential()
        return BlobServiceClient(account_url=url, credential=self._credential)

    @asynccontextmanager
    async def _blob(self, key: str, container: str | None) -> AsyncIterator[Any]:
        service = self._service_client()
        try:
            yield service.get_blob_client(container=container or self.default_container, blob=key)
        finally:
            await service.close()

    @asynccontextmanager
    async def _container(self, container: str | None) -> AsyncIterator[Any]:
        service = self._service_client()
        try:
            yield service.get_container_client(container or self.default_container)
        finally:
            await service.close()

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
        from azure.storage.blob import ContentSettings

        checksum = hashlib.sha256(data).hexdigest()
        try:
            async with self._blob(key, container) as blob:
                response = await blob.upload_blob(
                    data,
                    overwrite=True,  # stage re-runs rewrite the same key by design
                    content_settings=ContentSettings(content_type=content_type),
                    metadata={**(metadata or {}), "sha256": checksum},
                )
        except Exception as exc:
            raise StorageError(f"Azure upload failed for {key}: {exc}") from exc

        return UploadResult(
            key=key,
            size=len(data),
            etag=str(response.get("etag", "")).strip('"') or None,
            checksum=checksum,
            container=container or self.default_container,
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
        """Upload in blocks, staging then committing.

        Peak memory is one block regardless of file size, which is what makes a
        1,000-document batch of 150-page PDFs survivable in a worker pool.
        """
        import base64
        import uuid as uuid_module

        from azure.storage.blob import ContentSettings

        block_size = 4 * 1024 * 1024
        digest = hashlib.sha256()
        total = 0
        block_ids: list[str] = []

        try:
            async with self._blob(key, container) as blob:
                while True:
                    chunk = await stream.read(block_size)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    # Block ids must be equal-length base64 strings.
                    block_id = base64.b64encode(uuid_module.uuid4().bytes).decode()
                    await blob.stage_block(block_id=block_id, data=chunk)
                    block_ids.append(block_id)

                checksum = digest.hexdigest()
                if not block_ids:
                    # Empty stream: commit an empty blob rather than nothing at all,
                    # so callers see a consistent object.
                    await blob.upload_blob(
                        b"",
                        overwrite=True,
                        content_settings=ContentSettings(content_type=content_type),
                        metadata={**(metadata or {}), "sha256": checksum},
                    )
                else:
                    await blob.commit_block_list(
                        block_ids,
                        content_settings=ContentSettings(content_type=content_type),
                        metadata={**(metadata or {}), "sha256": checksum},
                    )
        except Exception as exc:
            # Uncommitted blocks are garbage-collected by Azure after seven days,
            # so there is nothing to clean up explicitly here.
            raise StorageError(f"Azure stream upload failed for {key}: {exc}") from exc

        return UploadResult(
            key=key,
            size=total,
            checksum=digest.hexdigest(),
            container=container or self.default_container,
        )

    # ------------------------------------------------------------------- read
    async def get_bytes(self, key: str, *, container: str | None = None) -> bytes:
        try:
            async with self._blob(key, container) as blob:
                downloader = await blob.download_blob()
                return bytes(await downloader.readall())
        except Exception as exc:
            if _is_missing(exc):
                raise NotFoundError("Object", key) from exc
            raise StorageError(f"Azure download failed for {key}: {exc}") from exc

    async def get_stream(
        self, key: str, *, container: str | None = None, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        try:
            async with self._blob(key, container) as blob:
                downloader = await blob.download_blob()
                async for chunk in downloader.chunks():
                    yield chunk
        except Exception as exc:
            if _is_missing(exc):
                raise NotFoundError("Object", key) from exc
            raise StorageError(f"Azure stream failed for {key}: {exc}") from exc

    async def head(self, key: str, *, container: str | None = None) -> ObjectMetadata | None:
        try:
            async with self._blob(key, container) as blob:
                props = await blob.get_blob_properties()
        except Exception as exc:
            if _is_missing(exc):
                return None
            raise StorageError(f"Azure head failed for {key}: {exc}") from exc

        return ObjectMetadata(
            key=key,
            size=int(props.size or 0),
            content_type=str(
                getattr(props.content_settings, "content_type", "application/octet-stream")
            ),
            etag=str(props.etag or "").strip('"') or None,
            last_modified=props.last_modified,
            checksum=(props.metadata or {}).get("sha256"),
            metadata=dict(props.metadata or {}),
        )

    async def exists(self, key: str, *, container: str | None = None) -> bool:
        try:
            async with self._blob(key, container) as blob:
                return bool(await blob.exists())
        except Exception as exc:
            raise StorageError(f"Azure exists check failed for {key}: {exc}") from exc

    # ----------------------------------------------------------------- delete
    async def delete(self, key: str, *, container: str | None = None) -> bool:
        try:
            async with self._blob(key, container) as blob:
                await blob.delete_blob()
            return True
        except Exception as exc:
            if _is_missing(exc):
                return False
            raise StorageError(f"Azure delete failed for {key}: {exc}") from exc

    async def delete_prefix(self, prefix: str, *, container: str | None = None) -> int:
        deleted = 0
        try:
            async with self._container(container) as client:
                batch: list[str] = []
                async for blob in client.list_blobs(name_starts_with=prefix):
                    batch.append(blob.name)
                    # Azure caps a batch delete at 256 sub-requests.
                    if len(batch) >= 256:
                        await client.delete_blobs(*batch)
                        deleted += len(batch)
                        batch = []
                if batch:
                    await client.delete_blobs(*batch)
                    deleted += len(batch)
        except Exception as exc:
            raise StorageError(f"Azure prefix delete failed for {prefix}: {exc}") from exc
        return deleted

    async def list_keys(
        self, prefix: str, *, container: str | None = None, limit: int = 1000
    ) -> list[str]:
        keys: list[str] = []
        try:
            async with self._container(container) as client:
                async for blob in client.list_blobs(name_starts_with=prefix):
                    keys.append(str(blob.name))
                    if len(keys) >= limit:
                        break
        except Exception as exc:
            raise StorageError(f"Azure list failed for {prefix}: {exc}") from exc
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
        """Generate a read-only SAS URL.

        Requires an account key or a user delegation key. Under managed identity a
        delegation key is requested from the service, so no secret is stored.
        """
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        ttl = expires_in or self.settings.signed_url_ttl_seconds
        expiry = datetime.now(UTC) + timedelta(seconds=ttl)
        bucket = container or self.default_container

        service = self._service_client()
        try:
            account_name = service.account_name
            account_key = self.settings.azure_account_key or getattr(
                service.credential, "account_key", None
            )

            sas_kwargs: dict[str, Any] = {
                "account_name": account_name,
                "container_name": bucket,
                "blob_name": key,
                "permission": BlobSasPermissions(read=True),
                "expiry": expiry,
            }
            if download_filename:
                sas_kwargs["content_disposition"] = f'attachment; filename="{download_filename}"'

            if account_key:
                sas_kwargs["account_key"] = account_key
            else:
                start = datetime.now(UTC) - timedelta(minutes=5)  # clock skew tolerance
                sas_kwargs["user_delegation_key"] = await service.get_user_delegation_key(
                    key_start_time=start, key_expiry_time=expiry
                )

            token = generate_blob_sas(**sas_kwargs)
            return f"https://{account_name}.blob.core.windows.net/{bucket}/{key}?{token}"
        except Exception as exc:
            raise StorageError(f"Could not sign URL for {key}: {exc}") from exc
        finally:
            await service.close()

    async def copy(
        self,
        source_key: str,
        dest_key: str,
        *,
        source_container: str | None = None,
        dest_container: str | None = None,
    ) -> UploadResult:
        source_url = await self.signed_url(source_key, container=source_container, expires_in=600)
        try:
            async with self._blob(dest_key, dest_container) as blob:
                await blob.start_copy_from_url(source_url)
        except Exception as exc:
            raise StorageError(f"Azure copy failed {source_key} -> {dest_key}: {exc}") from exc

        meta = await self.head(dest_key, container=dest_container)
        return UploadResult(
            key=dest_key,
            size=meta.size if meta else 0,
            etag=meta.etag if meta else None,
            checksum=meta.checksum if meta else None,
            container=dest_container or self.default_container,
        )

    # ----------------------------------------------------------------- health
    async def health(self) -> bool:
        try:
            async with self._container(None) as client:
                await client.get_container_properties()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("azure_storage_unhealthy", error=str(exc))
            return False

    async def close(self) -> None:
        if self._credential is not None:
            try:
                await self._credential.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("azure_credential_close_failed", error=str(exc))
            self._credential = None


def _is_missing(exc: Exception) -> bool:
    from contextlib import suppress

    with suppress(ImportError):
        from azure.core.exceptions import ResourceNotFoundError

        if isinstance(exc, ResourceNotFoundError):
            return True
    return getattr(exc, "status_code", None) == 404


__all__ = ["AzureBlobStorage"]

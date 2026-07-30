"""Storage factory.

``get_storage()`` is the only way business code obtains a storage handle, so the
concrete provider is decided in exactly one place. Adapters are imported lazily
so a deployment installs only the SDK it uses.
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import get_settings
from app.core.errors import StorageError
from app.core.logging import get_logger
from app.storage.base import (
    IObjectStorage,
    ObjectMetadata,
    StorageKey,
    SupportsAsyncRead,
    UploadResult,
)

logger = get_logger(__name__)


@lru_cache(maxsize=1)
def get_storage() -> IObjectStorage:
    """The configured storage adapter (cached per process)."""
    provider = get_settings().storage.provider

    if provider == "azure":
        from app.storage.azure import AzureBlobStorage

        storage: IObjectStorage = AzureBlobStorage()
    elif provider in {"s3", "minio"}:
        # MinIO is S3-compatible: same adapter, different endpoint + addressing.
        from app.storage.s3 import S3Storage

        storage = S3Storage()
        storage.provider = provider
    elif provider == "local":
        from app.storage.local import LocalStorage

        storage = LocalStorage()
    else:  # pragma: no cover - Literal type makes this unreachable
        raise StorageError(f"Unknown storage provider: {provider}")

    logger.info("storage_initialised", provider=provider)
    return storage


async def close_storage() -> None:
    """Release pooled clients and credentials on shutdown."""
    try:
        await get_storage().close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("storage_close_failed", error=str(exc))
    get_storage.cache_clear()


__all__ = [
    "IObjectStorage",
    "ObjectMetadata",
    "StorageKey",
    "SupportsAsyncRead",
    "UploadResult",
    "close_storage",
    "get_storage",
]

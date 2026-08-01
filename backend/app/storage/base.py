"""``IObjectStorage`` - the storage abstraction.

Azure Blob is the default target, but no business logic may know that. Everything
upstream (upload, artifacts, exports, the viewer's file streaming) talks to this
interface, so swapping to S3 or MinIO is a configuration change plus one adapter
and nothing else (§4).

Design decisions:

* **Streaming first.** ``put_stream``/``get_stream`` exist because a 150-page PDF
  and a 200 MB artifact must not be buffered whole in a worker's memory when the
  pipeline is running a thousand documents.
* **Signed URLs, not proxying.** ``signed_url`` lets the browser fetch a document
  straight from storage. Streaming every page render through the API would make
  the API the bottleneck for the PDF viewer.
* **Keys are built here.** :class:`StorageKey` owns the layout so paths are
  consistent and, critically, always project-prefixed - a stray key cannot land
  outside its project's prefix.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.core.enums import ArtifactKind


@dataclass(frozen=True, slots=True)
class ObjectMetadata:
    """What storage knows about a stored object."""

    key: str
    size: int
    content_type: str
    etag: str | None = None
    last_modified: datetime | None = None
    checksum: str | None = None
    metadata: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class UploadResult:
    key: str
    size: int
    etag: str | None = None
    checksum: str | None = None
    container: str | None = None


class StorageKey:
    """Builds storage keys. The single source of truth for object layout.

    Layout (every key is project-prefixed, which is what keeps one project's bytes
    from ever being addressable under another's prefix):

        projects/{project_id}/contracts/{contract_id}/v{version}/{filename}
        projects/{project_id}/contracts/{contract_id}/artifacts/{kind}/g{gen}.json
        projects/{project_id}/exports/{export_id}/{filename}
        projects/{project_id}/contracts/{contract_id}/pages/{page}.png
    """

    @staticmethod
    def _safe(name: str) -> str:
        """Strip anything that could escape the prefix or confuse a backend.

        Path traversal via a crafted filename is the obvious risk; a leading dot or
        a backslash on a Windows-hosted local adapter is the less obvious one.
        """
        import re

        cleaned = name.replace("\\", "/").split("/")[-1]
        cleaned = re.sub(r"[^A-Za-z0-9._\- ]+", "_", cleaned).strip(". ")
        return (cleaned or "file")[:255]

    @classmethod
    def contract_file(
        cls,
        project_id: uuid.UUID,
        contract_id: uuid.UUID,
        version: int,
        filename: str,
    ) -> str:
        return f"projects/{project_id}/contracts/{contract_id}/v{version}/{cls._safe(filename)}"

    @classmethod
    def artifact(
        cls,
        project_id: uuid.UUID,
        contract_id: uuid.UUID,
        kind: ArtifactKind | str,
        generation: int = 1,
    ) -> str:
        kind_value = kind.value if isinstance(kind, ArtifactKind) else str(kind)
        return (
            f"projects/{project_id}/contracts/{contract_id}"
            f"/artifacts/{kind_value}/g{generation}.json"
        )

    @classmethod
    def export(cls, project_id: uuid.UUID | None, export_id: uuid.UUID, filename: str) -> str:
        prefix = f"projects/{project_id}" if project_id else "application"
        return f"{prefix}/exports/{export_id}/{cls._safe(filename)}"

    @classmethod
    def page_image(cls, project_id: uuid.UUID, contract_id: uuid.UUID, page: int) -> str:
        return f"projects/{project_id}/contracts/{contract_id}/pages/{page:05d}.png"

    @classmethod
    def parser_cache(cls, parser: str, file_hash: str) -> str:
        """Cached raw parser response for a document, keyed by content hash.

        Deliberately **not** project-prefixed, unlike everything else here. The key
        is the SHA-256 of the file itself, so the same PDF uploaded to two projects
        - or re-uploaded after a delete - hits one cache entry and the layout
        service is paid for once. Nothing project-identifying is stored: the value
        is the vendor's rendering of bytes the uploader already possessed.

        The hash is the whole key, so a changed file cannot collide with a stale
        entry.
        """
        return f"parser-cache/{cls._safe(parser)}/{cls._safe(file_hash)}.json"

    @classmethod
    def contract_prefix(cls, project_id: uuid.UUID, contract_id: uuid.UUID) -> str:
        """Prefix covering everything derived from one contract - the delete scope."""
        return f"projects/{project_id}/contracts/{contract_id}/"

    @classmethod
    def project_prefix(cls, project_id: uuid.UUID) -> str:
        return f"projects/{project_id}/"


@runtime_checkable
class SupportsAsyncRead(Protocol):
    """Minimal async reader - what an adapter needs from an upload stream."""

    async def read(self, size: int = -1) -> bytes: ...


class IObjectStorage(ABC):
    """Object storage contract.

    Implementations must be safe to share across concurrent tasks and must not
    hold a connection open between calls, because worker pools call them from many
    tasks at once.
    """

    #: Adapter name, for logging and health reporting.
    provider: str = "abstract"

    # ------------------------------------------------------------------ write
    @abstractmethod
    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        container: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        """Store ``data`` at ``key``, overwriting any existing object.

        Overwrite rather than fail: stage re-runs are idempotent by design, so
        rewriting an artifact at the same key is the expected path (§10.1).
        """

    @abstractmethod
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
        """Store a stream without buffering it entirely in memory."""

    # ------------------------------------------------------------------- read
    @abstractmethod
    async def get_bytes(self, key: str, *, container: str | None = None) -> bytes:
        """Read an object whole. Raises :class:`~app.core.errors.NotFoundError`."""

    @abstractmethod
    def get_stream(
        self, key: str, *, container: str | None = None, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        """Stream an object in chunks - used to serve documents to the viewer.

        Declared ``def``, not ``async def``: every implementation is an async
        *generator*, which returns an ``AsyncIterator`` directly rather than a
        coroutine yielding one. Declaring it ``async def`` would make the interface
        promise a coroutine and break any caller that iterated it as documented.
        """

    @abstractmethod
    async def head(self, key: str, *, container: str | None = None) -> ObjectMetadata | None:
        """Object metadata, or ``None`` when absent."""

    @abstractmethod
    async def exists(self, key: str, *, container: str | None = None) -> bool: ...

    # ----------------------------------------------------------------- delete
    @abstractmethod
    async def delete(self, key: str, *, container: str | None = None) -> bool:
        """Delete one object. Returns ``False`` if it was already gone."""

    @abstractmethod
    async def delete_prefix(self, prefix: str, *, container: str | None = None) -> int:
        """Delete everything under a prefix. Returns the count removed."""

    @abstractmethod
    async def list_keys(
        self, prefix: str, *, container: str | None = None, limit: int = 1000
    ) -> list[str]: ...

    # ------------------------------------------------------------------ share
    @abstractmethod
    async def signed_url(
        self,
        key: str,
        *,
        container: str | None = None,
        expires_in: int | None = None,
        download_filename: str | None = None,
    ) -> str:
        """Time-limited URL granting direct read access.

        Lets the browser fetch a PDF straight from storage instead of proxying
        every byte through the API.
        """

    @abstractmethod
    async def copy(
        self,
        source_key: str,
        dest_key: str,
        *,
        source_container: str | None = None,
        dest_container: str | None = None,
    ) -> UploadResult: ...

    # ----------------------------------------------------------------- health
    @abstractmethod
    async def health(self) -> bool:
        """Can the backend be reached and written to?"""

    async def close(self) -> None:
        """Release any pooled clients. Called on application shutdown."""
        return None

    # -------------------------------------------------------------- JSON sugar
    async def put_json(
        self,
        key: str,
        payload: Any,
        *,
        container: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        """Store a JSON artifact and return its checksum.

        Every pipeline artifact goes through here, so the checksum recorded on
        ``document_artifacts`` is always computed the same way and stays
        comparable across regenerations.
        """
        import orjson

        from app.core.cache import _json_default

        data = orjson.dumps(payload, default=_json_default, option=orjson.OPT_SORT_KEYS)
        return await self.put_bytes(
            key,
            data,
            content_type="application/json",
            container=container,
            metadata=metadata,
        )

    async def get_json(self, key: str, *, container: str | None = None) -> Any:
        import orjson

        return orjson.loads(await self.get_bytes(key, container=container))


__all__ = [
    "IObjectStorage",
    "ObjectMetadata",
    "StorageKey",
    "SupportsAsyncRead",
    "UploadResult",
]

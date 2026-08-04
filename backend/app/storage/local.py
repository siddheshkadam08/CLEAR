"""Filesystem storage adapter - development and tests only.

Lets the whole pipeline run with no object-storage dependency. Production refuses
to start with ``STORAGE_PROVIDER=local`` (see the production guard in
:class:`~app.core.config.Settings`), because it offers no durability, no
replication and no real signed URLs.

Filesystem I/O is synchronous, so every operation runs in a worker thread via
``asyncio.to_thread``: a 200 MB read must not block the event loop that is also
serving requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

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

#: Windows' legacy 260-character path ceiling, which this module has to work under
#: because opting out system-wide is a registry change no test run should require.
_WINDOWS_MAX_PATH = 260


def _long_path_safe(path: Path) -> Path:
    """On Windows, rewrite an absolute path so ``MAX_PATH`` does not apply.

    A storage key is deep by design - ``projects/<uuid>/contracts/<uuid>/artifacts/
    <kind>/g1.json`` is 130-odd characters before the root - so a root that is
    itself nested puts ordinary writes over the 260-character limit. The failure is
    an unhelpful one: ``mkdir`` succeeds because the directory chain fits, then the
    file write fails with ``[Errno 2] No such file or directory`` (WinError 206),
    which reads as a missing directory rather than a path-length problem.

    The ``\\\\?\\`` prefix asks Win32 for the extended-length API, where the limit
    is ~32767 instead. It is a no-op on POSIX, and skipped for UNC paths, which
    need the different ``\\\\?\\UNC\\server\\share`` spelling and are not worth
    special-casing for a development-only adapter.

    Requires a fully-qualified, normalised path - the prefix disables the OS's own
    normalisation, so ``..`` would stop being collapsed. Every caller here passes a
    ``resolve()``-d path, which guarantees both.
    """
    if os.name != "nt":
        return path
    text = str(path)
    if text.startswith("\\\\?\\"):
        return path
    # Drive-letter paths only: `C:` is two characters, a UNC drive is not.
    if len(path.drive) != 2 or not path.drive.endswith(":"):
        return path
    return Path(f"\\\\?\\{text}")


class LocalStorage(IObjectStorage):
    provider = "local"

    def __init__(self, root: str | None = None) -> None:
        settings = get_settings()
        # Resolved once, so every derived path is absolute and normalised - which
        # is what `_long_path_safe` requires and what makes the containment check
        # in `_path` meaningful.
        self.root = _long_path_safe(Path(root or settings.storage.local_root).resolve())
        self.default_container = settings.storage.container
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def _bucket_root(self, container: str | None) -> Path:
        return (self.root / (container or self.default_container)).resolve()

    def _path(self, key: str, container: str | None) -> Path:
        """Resolve a key to a path, refusing anything that escapes the root.

        ``StorageKey`` already sanitises filenames, but this is the backstop: a key
        assembled elsewhere must not be able to traverse out of the storage root.

        The containment check runs on the resolved path *before* the long-path
        rewrite matters, and both sides are resolved the same way, so escaping via
        ``..`` is still caught - see `_long_path_safe` on why that ordering is not
        incidental.
        """
        root = self._bucket_root(container)
        target = (root / key).resolve()
        if not str(target).startswith(str(root)):
            raise StorageError(
                "Rejected a storage key that resolves outside the storage root.",
                details={"key": key},
            )
        return _long_path_safe(target)

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
        path = self._path(key, container)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temporary file then rename: a crash mid-write must not
            # leave a truncated artifact that later reads treat as valid.
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise StorageError(f"Could not write {key}: {exc}") from exc

        checksum = hashlib.sha256(data).hexdigest()
        return UploadResult(
            key=key,
            size=len(data),
            etag=checksum[:32],
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
        path = self._path(key, container)
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)

        tmp = path.with_suffix(path.suffix + ".tmp")
        digest = hashlib.sha256()
        total = 0

        try:
            handle = await asyncio.to_thread(tmp.open, "wb")
            try:
                while True:
                    chunk = await stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    total += len(chunk)
                    await asyncio.to_thread(handle.write, chunk)
            finally:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(tmp.replace, path)
        except OSError as exc:
            await asyncio.to_thread(tmp.unlink, True)
            raise StorageError(f"Could not write {key}: {exc}") from exc

        checksum = digest.hexdigest()
        return UploadResult(
            key=key,
            size=total,
            etag=checksum[:32],
            checksum=checksum,
            container=container or self.default_container,
        )

    # ------------------------------------------------------------------- read
    async def get_bytes(self, key: str, *, container: str | None = None) -> bytes:
        path = self._path(key, container)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError as exc:
            raise NotFoundError("Object", key) from exc
        except OSError as exc:
            raise StorageError(f"Could not read {key}: {exc}") from exc

    async def get_stream(
        self, key: str, *, container: str | None = None, chunk_size: int = 1024 * 1024
    ) -> AsyncIterator[bytes]:
        path = self._path(key, container)
        if not await asyncio.to_thread(path.exists):
            raise NotFoundError("Object", key)

        handle = await asyncio.to_thread(path.open, "rb")
        try:
            while True:
                chunk = await asyncio.to_thread(handle.read, chunk_size)
                if not chunk:
                    break
                yield chunk
        finally:
            await asyncio.to_thread(handle.close)

    async def head(self, key: str, *, container: str | None = None) -> ObjectMetadata | None:
        path = self._path(key, container)
        if not await asyncio.to_thread(path.exists):
            return None
        stat = await asyncio.to_thread(path.stat)
        return ObjectMetadata(
            key=key,
            size=stat.st_size,
            content_type="application/octet-stream",
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )

    async def exists(self, key: str, *, container: str | None = None) -> bool:
        return await asyncio.to_thread(self._path(key, container).exists)

    # ----------------------------------------------------------------- delete
    async def delete(self, key: str, *, container: str | None = None) -> bool:
        path = self._path(key, container)
        try:
            await asyncio.to_thread(path.unlink)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StorageError(f"Could not delete {key}: {exc}") from exc

    async def delete_prefix(self, prefix: str, *, container: str | None = None) -> int:
        base = self._path(prefix, container)

        def _remove() -> int:
            if base.is_dir():
                count = sum(1 for _ in base.rglob("*") if _.is_file())
                shutil.rmtree(base, ignore_errors=True)
                return count
            parent = base.parent
            if not parent.exists():
                return 0
            removed = 0
            for candidate in parent.glob(f"{base.name}*"):
                if candidate.is_file():
                    candidate.unlink(missing_ok=True)
                    removed += 1
            return removed

        return await asyncio.to_thread(_remove)

    async def list_keys(
        self, prefix: str, *, container: str | None = None, limit: int = 1000
    ) -> list[str]:
        # Same rewrite as `_path`, or `relative_to` below would compare a prefixed
        # path against an unprefixed root and raise.
        root = _long_path_safe(self._bucket_root(container))
        base = self._path(prefix, container)

        def _list() -> list[str]:
            search_root = base if base.is_dir() else base.parent
            if not search_root.exists():
                return []
            keys: list[str] = []
            for path in sorted(search_root.rglob("*")):
                if not path.is_file() or path.suffix == ".tmp":
                    continue
                key = str(path.relative_to(root)).replace("\\", "/")
                if key.startswith(prefix):
                    keys.append(key)
                if len(keys) >= limit:
                    break
            return keys

        return await asyncio.to_thread(_list)

    # ------------------------------------------------------------------ share
    async def signed_url(
        self,
        key: str,
        *,
        container: str | None = None,
        expires_in: int | None = None,
        download_filename: str | None = None,
    ) -> str:
        """Return an API-relative URL rather than a real signed URL.

        The local filesystem has nothing to sign against, so the caller is routed
        back through the authenticated file endpoint. Access control is therefore
        still enforced - it just costs a proxy hop, which is acceptable in
        development.
        """
        from urllib.parse import quote

        bucket = container or self.default_container
        return f"/api/v1/files/{bucket}/{quote(key, safe='/')}"

    async def copy(
        self,
        source_key: str,
        dest_key: str,
        *,
        source_container: str | None = None,
        dest_container: str | None = None,
    ) -> UploadResult:
        data = await self.get_bytes(source_key, container=source_container)
        return await self.put_bytes(dest_key, data, container=dest_container)

    # ----------------------------------------------------------------- health
    async def health(self) -> bool:
        try:
            probe = self.root / ".health"

            def _probe() -> None:
                probe.parent.mkdir(parents=True, exist_ok=True)
                probe.write_bytes(b"ok")
                probe.unlink(missing_ok=True)

            await asyncio.to_thread(_probe)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("local_storage_unhealthy", error=str(exc))
            return False


__all__ = ["LocalStorage"]

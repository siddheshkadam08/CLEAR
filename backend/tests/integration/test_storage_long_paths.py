"""Storage keys must survive a deep root on Windows.

The integration suite failed with ``WinError 206`` - "the filename or extension is
too long" - on Windows and nowhere else. The arithmetic is unforgiving rather than
exotic:

    C:\\Users\\<user>\\AppData\\Local\\Temp                        42
    \\pytest-of-<user>\\pytest-<n>\\<test-name>0                   ~63
    \\storage\\contracts                                           18
    projects/<uuid>/contracts/<uuid>/artifacts/<kind>/g1.json     ~127
    .tmp, from the write-then-rename                                4
                                                                 ----
                                                                  254+

Nothing there is unreasonable. Two UUIDs are 74 characters on their own, and the
key layout is production data - repathing it would orphan every artifact already
written. So the fix is to stop ``MAX_PATH`` applying, not to shorten the key.

The symptom deserves recording because it does not describe itself: the directory
chain fits under the limit and is created, then the file write fails with
``[Errno 2] No such file or directory`` naming a directory that exists. It reads
as a missing-directory bug anywhere except Windows.

These tests are meaningful on Windows and near-tautological on POSIX, where the
limit is ~4096. They are run on both anyway: the point is that the adapter behaves
identically, and a POSIX-only reading of `_long_path_safe` would be the way to
break that silently.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.storage.base import StorageKey
from app.storage.local import LocalStorage, _long_path_safe

WINDOWS_MAX_PATH = 260


def deep_root(tmp_path: Path) -> Path:
    """A root deep enough that a real storage key overruns ``MAX_PATH``.

    Padded to a fixed total rather than by a fixed amount, so the test means the
    same thing whatever `tmp_path` a machine hands out.
    """
    target = 150
    root = tmp_path / "storage"
    while len(str(root)) < target:
        root = root / "nested_directory"
    return root


@pytest.mark.asyncio
class TestDeepRoots:
    async def test_an_artifact_survives_a_root_that_overruns_max_path(
        self, tmp_path: Path
    ) -> None:
        root = deep_root(tmp_path)
        storage = LocalStorage(root=str(root))
        key = StorageKey.artifact(uuid.uuid4(), uuid.uuid4(), "normalized_document", 1)

        # The premise: without the extended-length rewrite this cannot be written.
        assert len(str(root / "contracts" / key)) > WINDOWS_MAX_PATH

        await storage.put_json(key, {"pages": [{"n": 1}]})

        assert await storage.get_json(key) == {"pages": [{"n": 1}]}

    async def test_the_key_is_listed_back_unprefixed(self, tmp_path: Path) -> None:
        """`list_keys` derives keys with `relative_to` against the bucket root.

        If the rewrite were applied to one side and not the other, that raises -
        or worse, leaks a ``\\\\?\\`` prefix into a key that then fails to match
        anything.
        """
        storage = LocalStorage(root=str(deep_root(tmp_path)))
        key = StorageKey.artifact(uuid.uuid4(), uuid.uuid4(), "canonical_document", 1)
        await storage.put_json(key, {"ok": True})

        listed = await storage.list_keys("projects/")

        assert key in listed
        assert not any("?" in entry for entry in listed)

    async def test_every_operation_agrees_about_the_path(self, tmp_path: Path) -> None:
        """head/exists/delete resolve keys separately from the write path."""
        storage = LocalStorage(root=str(deep_root(tmp_path)))
        key = StorageKey.artifact(uuid.uuid4(), uuid.uuid4(), "normalized_document", 2)
        await storage.put_json(key, {"ok": True})

        assert await storage.exists(key)
        meta = await storage.head(key)
        assert meta is not None and meta.size > 0
        assert await storage.delete(key)
        assert not await storage.exists(key)

    async def test_a_deep_stream_write_survives(self, tmp_path: Path) -> None:
        """`put_stream` builds its own `.tmp` sibling, so it needs the rewrite too."""

        class Source:
            def __init__(self) -> None:
                self._sent = False

            async def read(self, _size: int = -1) -> bytes:
                if self._sent:
                    return b""
                self._sent = True
                return b"contract bytes"

        storage = LocalStorage(root=str(deep_root(tmp_path)))
        key = StorageKey.artifact(uuid.uuid4(), uuid.uuid4(), "normalized_document", 3)

        await storage.put_stream(key, Source())

        assert await storage.get_bytes(key) == b"contract bytes"


@pytest.mark.asyncio
class TestContainmentStillHolds:
    """The rewrite must not cost the traversal guard.

    ``\\\\?\\`` disables the OS's own path normalisation, so a rewrite applied
    before the containment check - rather than after - would stop ``..`` being
    collapsed and turn a hardening measure into an escape hatch.
    """

    @pytest.mark.parametrize(
        "key",
        [
            "../../../etc/passwd",
            "projects/../../outside.json",
            "a/b/../../../../../../escape.json",
        ],
    )
    async def test_a_key_escaping_the_root_is_refused(self, tmp_path: Path, key: str) -> None:
        from app.core.errors import StorageError

        storage = LocalStorage(root=str(tmp_path / "storage"))

        with pytest.raises(StorageError):
            await storage.put_json(key, {"nope": True})

    async def test_an_ordinary_nested_key_is_still_allowed(self, tmp_path: Path) -> None:
        """The guard must reject traversal, not depth."""
        storage = LocalStorage(root=str(tmp_path / "storage"))

        await storage.put_json("projects/a/contracts/b/artifacts/c/g1.json", {"ok": True})

        assert await storage.exists("projects/a/contracts/b/artifacts/c/g1.json")


class TestLongPathSafe:
    """The rewrite itself, including the cases where it must decline."""

    def test_it_is_a_no_op_off_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Asserted by simulating POSIX rather than by running on it.

        CI is the only place this branch executes for real, which makes it the
        one branch a Windows-only change can break without anyone noticing until
        the pipeline goes red. Faking `os.name` costs nothing and moves the
        signal to the machine doing the editing.
        """
        monkeypatch.setattr("app.storage.local.os.name", "posix")
        path = Path("C:/would/be/rewritten/on/windows")

        assert _long_path_safe(path) == path

    @pytest.mark.skipif(os.name != "nt", reason="Windows path semantics.")
    def test_a_drive_path_is_prefixed(self) -> None:
        assert str(_long_path_safe(Path("C:/tmp/x").resolve())).startswith("\\\\?\\")

    @pytest.mark.skipif(os.name != "nt", reason="Windows path semantics.")
    def test_it_is_idempotent(self) -> None:
        """Applied twice - `_path` after `__init__` - must not double the prefix."""
        once = _long_path_safe(Path("C:/tmp/x").resolve())

        assert _long_path_safe(once) == once

    @pytest.mark.skipif(os.name != "nt", reason="Windows path semantics.")
    def test_a_unc_path_is_left_alone(self) -> None:
        """UNC needs `\\\\?\\UNC\\server\\share`; the plain prefix would corrupt it.

        Declining is the right call for a development-only adapter - a wrong
        prefix would break a network share that works today.
        """
        unc = Path("//server/share/storage")

        assert _long_path_safe(unc) == unc

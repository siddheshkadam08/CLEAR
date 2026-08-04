"""ZIP extraction, for uploads that arrive as an archive of contracts.

An archive is a delivery mechanism, not a document. It never becomes a contract:
it is unpacked, and each supported member becomes its own contract with its own
job, so one bad document cannot stop the rest.

Everything here runs on bytes already in memory and returns bytes, deliberately.
The members go straight into the existing per-file upload path, which already
knows how to hash, deduplicate, store, convert and enqueue - so an archived
document and a directly uploaded one take exactly the same route after this.

The guards are the interesting part. A ZIP is attacker-controlled input even when
the uploader is trusted, and two of its failure modes are silent:

* **Path traversal.** A member named ``../../etc/passwd`` is legal in the format.
  Nothing here writes to disk, but member names reach storage keys and log lines,
  so they are flattened to a basename rather than trusted.
* **Expansion.** A few hundred kilobytes can decompress to gigabytes. Both the
  member count and the total uncompressed size are capped, and the size is read
  from the central directory *before* decompressing anything.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field

from app.core.config import get_settings
from app.core.enums import FileType
from app.core.errors import ArchiveError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Directories some archivers add that never hold documents.
_JUNK_DIRECTORIES = ("__MACOSX/",)
_JUNK_NAMES = (".DS_Store", "Thumbs.db")


@dataclass(slots=True)
class ArchiveMember:
    """One supported document extracted from an archive."""

    file_name: str
    content: bytes
    file_type: FileType


@dataclass(slots=True)
class ArchiveContents:
    """What an archive yielded, including what was deliberately left out."""

    members: list[ArchiveMember] = field(default_factory=list)
    #: `(file_name, reason)` for everything skipped, so the response can say which
    #: files were ignored rather than silently dropping them.
    ignored: list[tuple[str, str]] = field(default_factory=list)


def _is_junk(name: str) -> bool:
    """Archiver debris, as opposed to a document the user chose not to send.

    The hidden-file rules apply to the *base name*, not the whole path. Testing
    the path would discard `./contracts/NDA.pdf` - a perfectly ordinary entry that
    several archivers produce - and `../NDA.pdf`, which should be flattened and
    kept rather than silently dropped. Traversal is neutralised by
    `safe_member_name`, so it does not need to be handled by refusing the file.
    """
    base = safe_member_name(name)
    return (
        name.startswith(_JUNK_DIRECTORIES)
        or base in _JUNK_NAMES
        # `._Foo.pdf` is an AppleDouble resource fork, not a document.
        or base.startswith("._")
        or not base
    )


def _disambiguate(name: str, original_path: str, taken: set[str]) -> str:
    """Make a flattened name unique without losing what distinguished it.

    Flattening `vendor-a/NDA.pdf` and `vendor-b/NDA.pdf` gives two members called
    `NDA.pdf`, and a ZIP of contracts filed per counterparty is exactly the shape
    that produces that. Nothing breaks - each becomes its own contract with its own
    id and storage key - but the two are indistinguishable in every list the user
    reads, which makes the upload useless for the case it was built for.

    So the folder that told them apart is folded back into the name, and only on
    collision: `vendor-b/NDA.pdf` becomes `vendor-b - NDA.pdf`. A numeric suffix is
    the last resort, for the genuinely ambiguous case of two identically-named
    files in identically-named folders.
    """
    if name not in taken:
        return name

    parts = original_path.replace("\\", "/").strip("/").split("/")
    stem, _, extension = name.rpartition(".")
    suffix = f".{extension}" if extension else ""
    stem = stem or name

    if len(parts) > 1:
        folder = parts[-2].strip()
        candidate = f"{folder} - {name}" if folder else name
        if candidate not in taken:
            return candidate

    counter = 2
    while f"{stem} ({counter}){suffix}" in taken:
        counter += 1
    return f"{stem} ({counter}){suffix}"


def safe_member_name(name: str) -> str:
    """Reduce an archive path to a bare file name.

    ``a/b/../../evil.pdf`` becomes ``evil.pdf``. Directory structure inside the
    archive is discarded rather than preserved: it carries no meaning for a
    contract, and keeping it means every consumer has to be trusted to sanitise
    it. Windows separators are handled too - a ZIP written on Windows may contain
    backslashes even though the format says otherwise.
    """
    return name.replace("\\", "/").rsplit("/", 1)[-1]


def extract(content: bytes, *, archive_name: str = "archive.zip") -> ArchiveContents:
    """Unpack an archive into its supported documents.

    Raises ``ArchiveError`` if the archive itself cannot be read - that fails the
    whole upload, because an archive that will not open has no members that could
    have succeeded. An archive that opens but contains nothing usable is *not* an
    error here; it returns empty ``members`` with populated ``ignored``, which the
    caller reports as a rejection naming the files it skipped.
    """
    settings = get_settings().upload
    max_total = settings.max_archive_uncompressed_mb * 1024 * 1024

    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(
            f"'{archive_name}' is not a readable ZIP archive.",
            details={"file_name": archive_name},
        ) from exc

    with archive:
        if (bad := archive.testzip()) is not None:
            raise ArchiveError(
                f"'{archive_name}' is corrupt - '{bad}' failed its checksum.",
                details={"file_name": archive_name, "member": bad},
            )

        entries = [info for info in archive.infolist() if not info.is_dir()]
        if len(entries) > settings.max_archive_members:
            raise ArchiveError(
                f"'{archive_name}' contains {len(entries)} files, more than the "
                f"{settings.max_archive_members} allowed in one archive.",
                details={"file_name": archive_name, "members": len(entries)},
            )

        # Read the declared sizes from the central directory before decompressing
        # anything - the point is to refuse the bomb, not to survive it.
        declared = sum(info.file_size for info in entries)
        if declared > max_total:
            raise ArchiveError(
                f"'{archive_name}' expands to "
                f"{declared // (1024 * 1024)} MB, more than the "
                f"{settings.max_archive_uncompressed_mb} MB allowed.",
                details={"file_name": archive_name, "uncompressed_bytes": declared},
            )

        contents = ArchiveContents()
        taken: set[str] = set()
        for info in entries:
            raw_name = info.filename
            if _is_junk(raw_name):
                continue

            name = _disambiguate(safe_member_name(raw_name), raw_name, taken)
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""

            if extension == "zip":
                # Not recursed into deliberately. Nesting is where expansion
                # guards get bypassed, and an archive of archives is not a
                # document-delivery shape worth supporting silently.
                contents.ignored.append((name, "nested archives are not unpacked"))
                continue

            try:
                file_type = FileType(extension)
            except ValueError:
                contents.ignored.append(
                    (name, f"'{extension or 'no extension'}' is not a supported document type")
                )
                continue

            try:
                member_bytes = archive.read(info)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                # RuntimeError is what zipfile raises for an encrypted member.
                contents.ignored.append((name, f"could not be read ({exc})"))
                continue

            if not member_bytes:
                contents.ignored.append((name, "the file is empty"))
                continue

            # Reserved only once the member is definitely being kept, so a skipped
            # file does not push a later one onto a disambiguated name.
            taken.add(name)
            contents.members.append(
                ArchiveMember(file_name=name, content=member_bytes, file_type=file_type)
            )

    logger.info(
        "archive_extracted",
        archive=archive_name,
        extracted=len(contents.members),
        ignored=len(contents.ignored),
    )
    return contents

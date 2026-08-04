"""ZIP extraction and Word conversion, at the upload boundary.

Two rules shape everything here:

* **The pipeline only ever sees a PDF.** Not a convenience - evidence highlighting
  positions a bounding box against a rendered page, so a clause extracted from a
  natively parsed DOCX could be searched but never *shown*. Converting at upload
  is what keeps every downstream stage unchanged.
* **An archive is a delivery mechanism, not a document.** A ZIP never becomes a
  contract. It is unpacked, and each supported member takes exactly the same route
  as a directly uploaded file - its own hash, contract, job and failure mode - so
  one bad document cannot stop the others.

The archive tests lean on hostile input, because a ZIP is attacker-controlled even
when the uploader is trusted and two of its failure modes are silent: path
traversal, and decompression bombs.
"""

from __future__ import annotations

import inspect
import io
import zipfile

import pytest

from app.core.enums import ArchiveType, ContractStatus, FileType
from app.core.errors import ArchiveError, ConversionError
from app.services import archive as archive_service
from app.services.conversion import DocumentConverter, pdf_name_for

PDF_BYTES = b"%PDF-1.4\n% minimal\n"
DOCX_BYTES = b"PK\x03\x04 pretend docx"


def build_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, payload in entries.items():
            handle.writestr(name, payload)
    return buffer.getvalue()


# =============================================================================
# What counts as a document
# =============================================================================
class TestFileTypes:
    def test_word_documents_need_conversion(self) -> None:
        assert FileType.DOC.needs_pdf_conversion
        assert FileType.DOCX.needs_pdf_conversion

    def test_a_pdf_does_not(self) -> None:
        """The whole point: the existing PDF path is untouched."""
        assert not FileType.PDF.needs_pdf_conversion

    def test_zip_is_not_a_file_type(self) -> None:
        """A contract row must never be able to hold `zip`.

        An archive is unpacked into contracts; it is not one. Keeping it out of
        `FileType` makes that unrepresentable rather than merely unlikely.
        """
        with pytest.raises(ValueError):
            FileType("zip")
        assert ArchiveType("zip") is ArchiveType.ZIP

    def test_conversion_failure_is_its_own_status(self) -> None:
        """Distinct from FAILED, which means processing ran and broke.

        Here the pipeline never started, so the remedy is a different source file
        rather than a retry - and the UI needs to say so.
        """
        assert ContractStatus.CONVERSION_FAILED != ContractStatus.FAILED


# =============================================================================
# Extraction
# =============================================================================
class TestArchiveExtraction:
    def test_supported_documents_are_extracted(self) -> None:
        content = build_zip({"NDA.pdf": PDF_BYTES, "MSA.docx": DOCX_BYTES})

        result = archive_service.extract(content)

        assert sorted(m.file_name for m in result.members) == ["MSA.docx", "NDA.pdf"]
        assert {m.file_type for m in result.members} == {FileType.PDF, FileType.DOCX}

    def test_unsupported_members_are_reported_not_dropped(self) -> None:
        """"Skip them and display a warning indicating which files were ignored."

        Silently ignoring is the failure mode worth guarding: the user sees a
        smaller contract count than files they packed and has no way to find out
        why.
        """
        content = build_zip({"NDA.pdf": PDF_BYTES, "notes.txt": b"x", "sheet.xlsx": b"PK\x03\x04"})

        result = archive_service.extract(content)

        assert [m.file_name for m in result.members] == ["NDA.pdf"]
        ignored = dict(result.ignored)
        assert set(ignored) == {"notes.txt", "sheet.xlsx"}
        assert all(reason for reason in ignored.values()), "each skip needs a reason"

    def test_directory_structure_is_flattened(self) -> None:
        content = build_zip({"2026/q1/NDA.pdf": PDF_BYTES})

        result = archive_service.extract(content)

        assert [m.file_name for m in result.members] == ["NDA.pdf"]

    def test_a_leading_dot_path_is_kept(self) -> None:
        """`./contracts/NDA.pdf` is an ordinary entry several archivers produce.

        The hidden-file rule applies to the base name; testing the whole path
        discarded these silently, which is indistinguishable from the file never
        having been in the archive.
        """
        content = build_zip({"./contracts/NDA.pdf": PDF_BYTES})

        result = archive_service.extract(content)

        assert [m.file_name for m in result.members] == ["NDA.pdf"]

    def test_archiver_debris_is_dropped_without_a_warning(self) -> None:
        """The user did not put these in, so reporting them is noise."""
        content = build_zip(
            {
                "NDA.pdf": PDF_BYTES,
                "__MACOSX/._NDA.pdf": b"junk",
                ".DS_Store": b"junk",
                "Thumbs.db": b"junk",
            }
        )

        result = archive_service.extract(content)

        assert [m.file_name for m in result.members] == ["NDA.pdf"]
        assert result.ignored == []

    def test_a_nested_archive_is_reported_rather_than_unpacked(self) -> None:
        """Recursion is where the expansion guards get bypassed."""
        content = build_zip({"inner.zip": build_zip({"NDA.pdf": PDF_BYTES})})

        result = archive_service.extract(content)

        assert result.members == []
        assert "nested" in dict(result.ignored)["inner.zip"]

    def test_duplicate_base_names_are_disambiguated(self) -> None:
        """A ZIP filed per counterparty repeats the base name, and flattening
        collapses them.

        Both still become separate contracts - different ids, different storage
        keys - but two rows called `NDA.pdf` are indistinguishable in every list
        the user reads, which makes the upload useless for the case it was built
        for. The folder that told them apart is folded back into the name.
        """
        content = build_zip(
            {"vendor-a/NDA.pdf": PDF_BYTES, "vendor-b/NDA.pdf": PDF_BYTES + b"different"}
        )

        names = [m.file_name for m in archive_service.extract(content).members]

        assert len(set(names)) == 2, "names must be distinguishable"
        assert "NDA.pdf" in names
        assert any("vendor-b" in name for name in names)

    def test_a_three_way_collision_still_resolves(self) -> None:
        content = build_zip(
            {f"vendor-{letter}/NDA.pdf": PDF_BYTES + letter.encode() for letter in "abc"}
        )

        names = [m.file_name for m in archive_service.extract(content).members]

        assert len(set(names)) == 3

    def test_names_that_do_not_collide_are_left_alone(self) -> None:
        """Disambiguation must not rename files that were already distinct."""
        content = build_zip({"a/NDA.pdf": PDF_BYTES, "b/MSA.pdf": PDF_BYTES + b"x"})

        names = sorted(m.file_name for m in archive_service.extract(content).members)

        assert names == ["MSA.pdf", "NDA.pdf"]

    def test_a_skipped_member_does_not_reserve_a_name(self) -> None:
        """Only kept files claim a name.

        Otherwise an ignored `notes.pdf` would push a real `notes.pdf` later in
        the archive onto a disambiguated name for no reason.
        """
        content = build_zip({"a/NDA.txt": b"x", "b/NDA.pdf": PDF_BYTES})

        names = [m.file_name for m in archive_service.extract(content).members]

        assert names == ["NDA.pdf"]

    def test_an_empty_member_is_skipped(self) -> None:
        content = build_zip({"NDA.pdf": PDF_BYTES, "blank.pdf": b""})

        result = archive_service.extract(content)

        assert [m.file_name for m in result.members] == ["NDA.pdf"]
        assert "empty" in dict(result.ignored)["blank.pdf"]


class TestArchiveIsHostileInput:
    @pytest.mark.parametrize(
        ("packed", "expected"),
        [
            ("../../../etc/passwd", "passwd"),
            ("../evil.pdf", "evil.pdf"),
            (r"a\b\evil.pdf", "evil.pdf"),
            ("nested/deep/../../escape.pdf", "escape.pdf"),
        ],
    )
    def test_a_traversal_path_is_reduced_to_a_bare_name(
        self, packed: str, expected: str
    ) -> None:
        """Member names reach storage keys and log lines, so they are not trusted.

        Flattening rather than rejecting: the *file* is legitimate, only its
        recorded path is not, and refusing it would lose a document the user
        meant to send.
        """
        assert archive_service.safe_member_name(packed) == expected

    def test_a_corrupt_archive_fails_the_upload(self) -> None:
        """An archive that will not open has no members that could have succeeded."""
        with pytest.raises(ArchiveError):
            archive_service.extract(b"this is not a zip file at all")

    def test_too_many_members_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings.upload, "max_archive_members", 2, raising=False)
        content = build_zip({f"doc{i}.pdf": PDF_BYTES for i in range(5)})

        with pytest.raises(ArchiveError, match="more than"):
            archive_service.extract(content)

    def test_an_oversized_expansion_is_refused_before_decompressing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zip bomb is a few hundred kilobytes that expands to gigabytes.

        The declared sizes come from the central directory, so this refuses the
        archive rather than surviving it.
        """
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings.upload, "max_archive_uncompressed_mb", 1, raising=False)
        content = build_zip({"huge.pdf": b"%PDF-" + b"\0" * (2 * 1024 * 1024)})

        with pytest.raises(ArchiveError, match="expands to"):
            archive_service.extract(content)

    def test_an_archive_with_nothing_usable_is_not_an_error(self) -> None:
        """It is a rejection the caller phrases, naming the files it skipped.

        Raising here would lose the list of what was in it, which is the only
        thing that makes the rejection actionable.
        """
        result = archive_service.extract(build_zip({"a.txt": b"x", "b.csv": b"y"}))

        assert result.members == []
        assert len(result.ignored) == 2


# =============================================================================
# The two hashes
# =============================================================================
class TestHashColumnsAnswerDifferentQuestions:
    """`sha256_hash` and `processing_sha256` must not be collapsed into one.

    They look redundant and are not:

    * `sha256_hash` is the identity of *the document the user uploaded*. It backs
      `uq_contracts_project_id_sha256_hash`, so it decides whether an upload is a
      duplicate. It has to be the original's, because LibreOffice does not produce
      byte-identical PDFs from one run to the next - hashing the conversion would
      let the same DOCX in over and over.
    * `processing_sha256` is the integrity of *the bytes at `storage_path`*. The
      validation stage re-computes it to prove storage has not corrupted or
      swapped the file.

    For a PDF upload they are equal, which is exactly why collapsing them passed
    every unit test and every PDF upload. It failed only on a real Word document:
    validation hashed the converted PDF, compared it against the DOCX's hash, and
    halted the job at the first stage with "the stored file does not match the
    hash recorded at upload" - an integrity alarm for a perfectly intact file.
    """

    def test_validation_prefers_the_processing_hash(self) -> None:
        from app.orchestrator.stages import validation as validation_stage

        source = inspect.getsource(validation_stage)

        assert "processing_sha256" in source, (
            "validation must compare against the hash of the file it just read"
        )

    def test_validation_still_falls_back_for_older_rows(self) -> None:
        """`processing_sha256` is NULL on contracts predating conversion.

        Those were processed exactly as uploaded, so `sha256_hash` describes the
        same bytes - and without the fallback every one of them would fail
        validation on reprocessing.
        """
        from app.orchestrator.stages import validation as validation_stage

        source = inspect.getsource(validation_stage)

        assert "contract.processing_sha256 or contract.sha256_hash" in source

    def test_the_upload_records_both(self) -> None:
        from app.services import upload as upload_service

        source = inspect.getsource(upload_service)

        assert "sha256_hash=validated.sha256" in source, "identity is the original's"
        assert "processing_sha256=processed.sha256" in source, "integrity is the PDF's"


# =============================================================================
# Conversion
# =============================================================================
class TestConversion:
    def test_the_converted_name_keeps_the_stem(self) -> None:
        assert pdf_name_for("Master Agreement.docx") == "Master Agreement.pdf"
        assert pdf_name_for("Deed.doc") == "Deed.pdf"
        assert pdf_name_for("no-extension") == "no-extension.pdf"

    @pytest.mark.asyncio
    async def test_converting_a_pdf_is_refused(self) -> None:
        """Calling this on a PDF is a bug in the caller, not a no-op."""
        with pytest.raises(ConversionError):
            await DocumentConverter().to_pdf(PDF_BYTES, "a.pdf", FileType.PDF)

    @pytest.mark.asyncio
    async def test_a_missing_engine_is_reported_as_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And says PDF uploads still work, because they do.

        Without that, "conversion failed" on a Word upload reads as the whole
        upload feature being broken.
        """
        monkeypatch.setattr("app.services.conversion.find_libreoffice", lambda: None)

        with pytest.raises(ConversionError) as caught:
            await DocumentConverter().to_pdf(DOCX_BYTES, "a.docx", FileType.DOCX)

        assert "LibreOffice" in caught.value.message
        assert "PDF uploads are unaffected" in caught.value.message
        # Retrying identical bytes against a still-absent binary cannot succeed.
        assert caught.value.retryable is False

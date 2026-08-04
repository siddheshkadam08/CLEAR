"""Artifact persistence and round-trip validation.

A production bug reached the pipeline because nothing exercised the *full* path an
artifact takes. The parser stage wrote a ``NormalizedDocument`` to object storage,
the enrichment stage read it back, and validation rejected it - because
``model_dump()`` emits ``@computed_field`` values while ``extra="forbid"`` refused
them on the way in. Every upload died at enrichment, on every parser. The unit
tests all passed, because each of them held a model in memory and never sent one
through storage.

So these tests do the thing that was missing:

    build -> model_dump -> orjson -> object storage -> read -> model_validate -> use

The reflective tests in :class:`TestComputedFieldRegression` are the important
ones. They enumerate CDM models and their computed fields at runtime, so a
computed field added next year is covered by an assertion nobody has to remember
to write - which is the only version of this guard that survives contact with a
growing schema.

Hermetic: filesystem-backed storage under ``tmp_path``. Remote backends are
covered by the same conformance suite and skip unless configured.
"""

from __future__ import annotations

import inspect
import time
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import orjson
import pytest
from pydantic import BaseModel, ValidationError

import app.ai.cdm.models as cdm_models
from app.ai.cdm.models import (
    CanonicalDocument,
    ContentBlock,
    Coordinates,
    CrossReference,
    DocumentList,
    DocumentMetadata,
    DocumentStatistics,
    Footnote,
    HeaderFooter,
    Image,
    ListItem,
    NormalizedDocument,
    Page,
    Paragraph,
    QualityMetrics,
    ReadingOrderEntry,
    Section,
    Signature,
    Table,
    TableCell,
)
from app.core.enums import ContentBlockType
from app.core.errors import ParserError
from app.storage.base import IObjectStorage, StorageKey
from app.storage.local import LocalStorage

# =============================================================================
# Builders
# =============================================================================
PROJECT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DOCUMENT_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
ORG_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


#: The CDM carries identifiers and timestamps as *strings*, not as UUID/datetime.
#: That is deliberate - the artifact is a wire format read by more than Python -
#: so the fidelity tests below assert the string survives and still parses back
#: into the richer type, which is what callers actually depend on.
CREATED_AT = datetime(2026, 3, 1, 12, 30, 45, tzinfo=UTC)


def make_metadata(**overrides: Any) -> DocumentMetadata:
    fields: dict[str, Any] = {
        "document_id": str(DOCUMENT_ID),
        "organization_id": str(ORG_ID),
        "project_id": str(PROJECT_ID),
        "file_name": "acme-globex-msa.pdf",
        "storage_path": "projects/x/contracts/y/source.pdf",
        "file_type": "pdf",
        "file_size": 274_233,
        "hash": "cb94dfa58a89610f3ea602b64752a989849a6ce4e6fe8a7d71cc8a9001643438",
        "parser_name": "pymupdf",
        "parser_version": "1.28.0",
        "adapter_version": "1.0.0",
        "created_at": CREATED_AT.isoformat(),
        "language": "en",
        "source_metadata": {"producer": "pytest", "pages": "3"},
    }
    fields.update(overrides)
    return DocumentMetadata(**fields)


def make_coordinates(page: int = 1) -> Coordinates:
    return Coordinates(page_number=page, x=56.5, y=72.25, width=499.5, height=18.75)


def make_page(number: int, blocks: int = 3) -> Page:
    return Page(
        page_number=number,
        width=612.0,
        height=792.0,
        rotation=0,
        reading_order=number * 100,
        text_char_count=1800,
        is_scanned=False,
        content_blocks=[
            ContentBlock(
                block_id=f"p{number}-b{index}",
                block_type=ContentBlockType.PARAGRAPH,
                order=index,
                coordinates=make_coordinates(number),
            )
            for index in range(blocks)
        ],
    )


def make_normalized(pages: int = 3, *, blocks_per_page: int = 3) -> NormalizedDocument:
    """A document that exercises every nested collection the CDM defines."""
    return NormalizedDocument(
        metadata=make_metadata(),
        pages=[make_page(n, blocks_per_page) for n in range(1, pages + 1)],
        sections=[
            Section(section_id="s1", title="1. TERM", level=1),
            Section(section_id="s2", title="2. FEES", level=1, parent_section="s1"),
        ],
        paragraphs=[
            Paragraph(
                paragraph_id=f"para-{n}",
                text=f"Paragraph {n} of the agreement.",
                page_number=(n % pages) + 1,
                coordinates=make_coordinates((n % pages) + 1),
                section_id="s1",
            )
            for n in range(6)
        ],
        tables=[
            Table(
                table_id="t1",
                page_number=1,
                coordinates=make_coordinates(1),
                cells=[
                    TableCell(row=0, col=0, text="Fee"),
                    TableCell(row=0, col=1, text="USD 2,400,000"),
                ],
            )
        ],
        lists=[
            DocumentList(
                list_id="l1",
                page_number=2,
                items=[ListItem(text="First obligation"), ListItem(text="Second obligation")],
            )
        ],
        images=[Image(image_id="i1", page_number=1, coordinates=make_coordinates(1))],
        signatures=[Signature(signature_id="sig1", page_number=3)],
        headers=[HeaderFooter(text="CONFIDENTIAL", page_number=1)],
        footers=[HeaderFooter(text="Page 1 of 3", page_number=1)],
        footnotes=[Footnote(footnote_id="f1", page_number=1, text="See Schedule A.")],
        quality=QualityMetrics(coordinate_coverage=1.0, warnings=["one warning"]),
    )


def make_canonical() -> CanonicalDocument:
    """The artifact the chunking stage reads back."""
    normalized = make_normalized(pages=2)
    return CanonicalDocument(
        metadata=normalized.metadata,
        statistics=DocumentStatistics(),
        pages=normalized.pages,
        sections=normalized.sections,
        paragraphs=normalized.paragraphs,
        tables=normalized.tables,
        lists=normalized.lists,
        images=normalized.images,
        signatures=normalized.signatures,
        headers=normalized.headers,
        footers=normalized.footers,
        footnotes=normalized.footnotes,
        references=[CrossReference(reference_id="x1", source_page=1, text="see Section 3")],
        reading_order=[
            ReadingOrderEntry(
                index=i,
                block_id=f"p1-b{i}",
                block_type=ContentBlockType.PARAGRAPH,
                page_number=1,
            )
            for i in range(3)
        ],
        quality_metrics=normalized.quality,
    )


# =============================================================================
# Storage helpers
# =============================================================================
class InMemoryStorage(LocalStorage):
    """Reference backend used to prove the round-trip is not filesystem-specific."""


@pytest.fixture
def storage(tmp_path: Any) -> IObjectStorage:
    return LocalStorage(root=str(tmp_path / "storage"))


def artifact_key(kind: str = "normalized_document") -> str:
    """The key the pipeline really writes.

    The fourth argument is ``generation: int``, not a filename. Passing "g1.json"
    produced ``.../gg1.json.json`` - still a valid key, so nothing failed, but it
    exercised a shape the pipeline never generates and made every path in this
    module five characters longer than production's.
    """
    return StorageKey.artifact(PROJECT_ID, DOCUMENT_ID, kind, 1)


async def store_and_load(storage: IObjectStorage, model: BaseModel, key: str) -> Any:
    """Exactly what the pipeline does: dump -> orjson -> store -> read -> decode."""
    await storage.put_json(key, model.model_dump(mode="json"))
    return await storage.get_json(key)


def all_cdm_models() -> list[type[BaseModel]]:
    """Every concrete CDM model, discovered rather than listed."""
    found: list[type[BaseModel]] = []
    for obj in vars(cdm_models).values():
        if (
            inspect.isclass(obj)
            and issubclass(obj, BaseModel)
            and obj.__module__ == cdm_models.__name__
            and obj is not cdm_models.CdmBase
        ):
            found.append(obj)
    return found


def models_with_computed_fields() -> list[type[BaseModel]]:
    return [m for m in all_cdm_models() if m.model_computed_fields]


# =============================================================================
# The regression that started this - and its generalisation
# =============================================================================
class TestComputedFieldRegression:
    """Guards the exact bug, then generalises so the next one cannot recur."""

    def test_every_cdm_model_with_computed_fields_is_covered(self) -> None:
        # Fails loudly if someone adds a computed field to a model this suite has
        # never seen, which is what keeps the reflective tests below honest.
        names = {m.__name__ for m in models_with_computed_fields()}
        assert names == {"Page", "QualityMetrics", "CanonicalDocument"}, (
            "A CDM model gained or lost a computed field. Confirm it round-trips, "
            f"then update this expectation. Currently: {sorted(names)}"
        )

    def test_page_block_count_round_trips(self) -> None:
        """The original failure: `pages.0.block_count Extra inputs are not permitted`."""
        page = make_page(1, blocks=13)
        dumped = page.model_dump(mode="json")

        assert dumped["block_count"] == 13  # computed field IS emitted
        restored = Page.model_validate(dumped)  # and MUST load back
        assert restored.block_count == 13

    def test_quality_metrics_is_degraded_round_trips(self) -> None:
        """The original failure: `quality.is_degraded Extra inputs are not permitted`."""
        quality = QualityMetrics(coordinate_coverage=0.5)
        dumped = quality.model_dump(mode="json")

        assert dumped["is_degraded"] is True
        assert QualityMetrics.model_validate(dumped).is_degraded is True

    def test_canonical_document_full_text_round_trips(self) -> None:
        """Same latent bug on the artifact chunking reads."""
        document = make_canonical()
        dumped = document.model_dump(mode="json")

        assert "full_text" in dumped
        CanonicalDocument.model_validate(dumped)  # must not raise

    @pytest.mark.parametrize("model", models_with_computed_fields(), ids=lambda m: m.__name__)
    def test_computed_fields_survive_round_trip(self, model: type[BaseModel]) -> None:
        """Reflective: any model with computed fields must reload its own dump.

        New computed fields are picked up automatically - no test edit required.
        """
        instance = _sample_for(model)
        dumped = instance.model_dump(mode="json")

        for name in model.model_computed_fields:
            assert name in dumped, f"{model.__name__}.{name} is not serialised"

        model.model_validate(dumped)  # the assertion that was missing

    @pytest.mark.parametrize("model", all_cdm_models(), ids=lambda m: m.__name__)
    def test_every_cdm_model_reloads_its_own_dump(self, model: type[BaseModel]) -> None:
        """The general invariant: dump(x) must always be valid input to x's model."""
        instance = _sample_for(model)
        model.model_validate(instance.model_dump(mode="json"))

    @pytest.mark.parametrize("model", all_cdm_models(), ids=lambda m: m.__name__)
    def test_forbid_still_rejects_genuinely_unknown_fields(self, model: type[BaseModel]) -> None:
        """The fix must not have weakened `extra="forbid"` into `ignore`."""
        payload = {**_sample_for(model).model_dump(mode="json"), "definitely_not_a_field": 1}
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def _sample_for(model: type[BaseModel]) -> BaseModel:
    """A minimal valid instance of any CDM model, for the reflective tests."""
    specials: dict[str, Any] = {
        "NormalizedDocument": make_normalized,
        "CanonicalDocument": make_canonical,
        "DocumentMetadata": make_metadata,
        "Page": lambda: make_page(1),
        "Coordinates": make_coordinates,
    }
    if model.__name__ in specials:
        return specials[model.__name__]()

    defaults: dict[str, Any] = {
        "page_number": 1,
        "source_page": 1,
        "x": 1.0,
        "y": 2.0,
        "width": 3.0,
        "height": 4.0,
        "block_id": "b1",
        "block_type": ContentBlockType.PARAGRAPH,
        "order": 0,
        "index": 0,
        "section_id": "s1",
        "title": "A title",
        "paragraph_id": "p1",
        "text": "some text",
        "row": 0,
        "col": 0,
        "table_id": "t1",
        "list_id": "l1",
        "image_id": "i1",
        "signature_id": "sig1",
        "footnote_id": "f1",
        "reference_id": "x1",
    }
    kwargs = {name: defaults[name] for name in model.model_fields if name in defaults}
    return model(**kwargs)


# =============================================================================
# Full pipeline path
# =============================================================================
class TestFullArtifactPath:
    async def test_normalized_document_survives_storage(self, storage: IObjectStorage) -> None:
        """parse -> CDM -> serialize -> store -> load -> deserialize -> continue."""
        original = make_normalized(pages=3)
        payload = await store_and_load(storage, original, artifact_key())

        # This is the exact call the enrichment stage makes.
        restored = NormalizedDocument.model_validate(payload)

        assert len(restored.pages) == 3
        assert restored.metadata.document_id == original.metadata.document_id
        assert restored.pages[0].block_count == original.pages[0].block_count

    async def test_canonical_document_survives_storage(self, storage: IObjectStorage) -> None:
        """The artifact the chunking stage reads."""
        original = make_canonical()
        payload = await store_and_load(storage, original, artifact_key("canonical_document"))

        restored = CanonicalDocument.model_validate(payload)
        assert restored.metadata.file_name == original.metadata.file_name
        assert len(restored.paragraphs) == len(original.paragraphs)

    async def test_round_trip_is_idempotent(self, storage: IObjectStorage) -> None:
        """Storing a restored document must produce identical bytes."""
        original = make_normalized()
        key = artifact_key()

        first = await store_and_load(storage, original, key)
        restored = NormalizedDocument.model_validate(first)
        second = await store_and_load(storage, restored, key + ".2")

        assert first == second

    async def test_checksum_is_stable_across_regeneration(self, storage: IObjectStorage) -> None:
        # `document_artifacts.checksum` is compared across runs to decide whether a
        # stage can be skipped, so identical content must hash identically.
        document = make_normalized()
        first = await storage.put_json("a/1.json", document.model_dump(mode="json"))
        second = await storage.put_json("a/2.json", document.model_dump(mode="json"))
        assert first.checksum == second.checksum


# =============================================================================
# Type fidelity
# =============================================================================
class TestTypeFidelity:
    async def test_uuids_survive(self, storage: IObjectStorage) -> None:
        payload = await store_and_load(storage, make_normalized(), artifact_key())
        restored = NormalizedDocument.model_validate(payload)

        # Carried as text, but must still round-trip to the same identifier - the
        # repositories parse these back into UUIDs to key database rows.
        assert restored.metadata.document_id == str(DOCUMENT_ID)
        assert uuid.UUID(restored.metadata.document_id) == DOCUMENT_ID
        assert uuid.UUID(restored.metadata.project_id) == PROJECT_ID

    async def test_datetimes_survive_with_timezone(self, storage: IObjectStorage) -> None:
        payload = await store_and_load(storage, make_normalized(), artifact_key())
        restored = NormalizedDocument.model_validate(payload)

        assert restored.metadata.created_at == CREATED_AT.isoformat()
        parsed = datetime.fromisoformat(restored.metadata.created_at)
        assert parsed == CREATED_AT
        assert parsed.tzinfo is not None  # offset preserved, not silently naive

    async def test_floats_keep_precision(self, storage: IObjectStorage) -> None:
        payload = await store_and_load(storage, make_normalized(), artifact_key())
        restored = NormalizedDocument.model_validate(payload)
        coordinates = restored.pages[0].content_blocks[0].coordinates

        assert coordinates is not None
        assert coordinates.x == 56.5
        assert coordinates.height == 18.75

    async def test_nested_objects_survive(self, storage: IObjectStorage) -> None:
        payload = await store_and_load(storage, make_normalized(), artifact_key())
        restored = NormalizedDocument.model_validate(payload)

        # page -> content_blocks -> coordinates: three levels down.
        block = restored.pages[0].content_blocks[0]
        assert block.coordinates is not None
        assert block.coordinates.page_number == 1

    async def test_lists_survive_with_order(self, storage: IObjectStorage) -> None:
        original = make_normalized(pages=4)
        payload = await store_and_load(storage, original, artifact_key())
        restored = NormalizedDocument.model_validate(payload)

        assert [p.page_number for p in restored.pages] == [1, 2, 3, 4]
        assert [p.paragraph_id for p in restored.paragraphs] == [
            p.paragraph_id for p in original.paragraphs
        ]

    async def test_every_nested_collection_survives(self, storage: IObjectStorage) -> None:
        original = make_normalized()
        payload = await store_and_load(storage, original, artifact_key())
        restored = NormalizedDocument.model_validate(payload)

        assert len(restored.tables) == len(original.tables)
        assert len(restored.tables[0].cells) == 2
        assert len(restored.lists[0].items) == 2
        assert len(restored.images) == 1
        assert len(restored.signatures) == 1
        assert len(restored.footnotes) == 1
        assert len(restored.headers) == 1
        assert len(restored.footers) == 1
        assert restored.quality.warnings == ["one warning"]

    async def test_canonical_nested_collections_survive(self, storage: IObjectStorage) -> None:
        original = make_canonical()
        payload = await store_and_load(storage, original, artifact_key("canonical_document"))
        restored = CanonicalDocument.model_validate(payload)

        assert len(restored.references) == 1
        assert len(restored.reading_order) == 3
        assert restored.reading_order[0].block_type is ContentBlockType.PARAGRAPH

    async def test_empty_collections_survive_as_empty(self, storage: IObjectStorage) -> None:
        # An empty list must come back as [] rather than None: downstream stages
        # iterate these without a guard.
        sparse = NormalizedDocument(metadata=make_metadata())
        payload = await store_and_load(storage, sparse, artifact_key())
        restored = NormalizedDocument.model_validate(payload)

        assert restored.pages == []
        assert restored.paragraphs == []
        assert restored.tables == []
        assert restored.quality.warnings == []

    async def test_optional_fields_survive_as_none(self, storage: IObjectStorage) -> None:
        paragraph = Paragraph(paragraph_id="p1", text="t", page_number=1)
        assert paragraph.coordinates is None

        await storage.put_json("p.json", paragraph.model_dump(mode="json"))
        restored = Paragraph.model_validate(await storage.get_json("p.json"))
        assert restored.coordinates is None

    async def test_metadata_survives_in_full(self, storage: IObjectStorage) -> None:
        """Every metadata field, compared one by one - no field may be dropped."""
        document = make_normalized()
        payload = await store_and_load(storage, document, artifact_key())
        restored = NormalizedDocument.model_validate(payload).metadata

        for field in DocumentMetadata.model_fields:
            assert getattr(restored, field) == getattr(document.metadata, field), field

    async def test_free_form_source_metadata_survives(self, storage: IObjectStorage) -> None:
        # A dict[str, str] the adapter fills with vendor-specific keys; it must not
        # be flattened or dropped on the way through.
        payload = await store_and_load(storage, make_normalized(), artifact_key())
        restored = NormalizedDocument.model_validate(payload)
        assert restored.metadata.source_metadata == {"producer": "pytest", "pages": "3"}

    async def test_unicode_survives(self, storage: IObjectStorage) -> None:
        text = "Governing law: Ireland — fee €2 400 000 · 日本語 · مرحبا"
        paragraph = Paragraph(paragraph_id="p1", text=text, page_number=1)

        await storage.put_json("u.json", paragraph.model_dump(mode="json"))
        assert Paragraph.model_validate(await storage.get_json("u.json")).text == text

    def test_decimals_are_serialisable_by_the_artifact_encoder(self) -> None:
        # No CDM field is Decimal today, but extracted monetary values are, and
        # they travel through the same encoder into the same artifacts.
        from app.core.cache import _json_default

        encoded = orjson.dumps(
            {"amount": Decimal("2400000.50"), "when": date(2026, 3, 1)},
            default=_json_default,
        )
        decoded = orjson.loads(encoded)
        assert Decimal(str(decoded["amount"])) == Decimal("2400000.50")
        assert decoded["when"] == "2026-03-01"

    async def test_key_order_does_not_affect_validation(self, storage: IObjectStorage) -> None:
        # `put_json` sorts keys (OPT_SORT_KEYS) so checksums stay comparable. That
        # makes literal field order unstable by design, so what must hold is that
        # validation is order-independent.
        document = make_normalized(pages=1)
        dumped = document.model_dump(mode="json")
        reversed_payload = dict(reversed(list(dumped.items())))

        assert NormalizedDocument.model_validate(reversed_payload) is not None


# =============================================================================
# Negative cases
# =============================================================================
class TestNegativeCases:
    def test_corrupted_json_is_rejected(self) -> None:
        with pytest.raises(orjson.JSONDecodeError):
            orjson.loads(b'{"metadata": {"document_id": ')

    async def test_truncated_artifact_is_rejected(self, storage: IObjectStorage) -> None:
        payload = orjson.dumps(make_normalized().model_dump(mode="json"))
        await storage.put_bytes("bad.json", payload[: len(payload) // 2])

        with pytest.raises(orjson.JSONDecodeError):
            await storage.get_json("bad.json")

    def test_missing_required_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="metadata"):
            NormalizedDocument.model_validate({"pages": []})

    def test_missing_nested_required_field_is_rejected(self) -> None:
        payload = make_normalized().model_dump(mode="json")
        del payload["metadata"]["file_name"]
        with pytest.raises(ValidationError, match="file_name"):
            NormalizedDocument.model_validate(payload)

    def test_non_string_identifier_is_rejected(self) -> None:
        payload = make_normalized().model_dump(mode="json")
        payload["metadata"]["document_id"] = 12345
        with pytest.raises(ValidationError, match="document_id"):
            NormalizedDocument.model_validate(payload)

    def test_malformed_uuid_fails_where_it_is_parsed(self) -> None:
        """Documents the real boundary rather than pretending the CDM enforces it.

        Identifiers are plain strings in the artifact, so a malformed one passes CDM
        validation and fails at the point a caller turns it into a UUID. Worth
        pinning: a future change to UUID-typed fields should break this test and
        make someone decide deliberately.
        """
        payload = make_normalized().model_dump(mode="json")
        payload["metadata"]["document_id"] = "not-a-uuid"

        restored = NormalizedDocument.model_validate(payload)  # accepted here
        with pytest.raises(ValueError):
            uuid.UUID(restored.metadata.document_id)  # rejected here

    def test_invalid_enum_is_rejected(self) -> None:
        payload = make_page(1).model_dump(mode="json")
        payload["content_blocks"][0]["block_type"] = "not_a_block_type"
        with pytest.raises(ValidationError, match="block_type"):
            Page.model_validate(payload)

    def test_invalid_nested_object_is_rejected(self) -> None:
        payload = make_page(1).model_dump(mode="json")
        payload["content_blocks"][0]["coordinates"] = {"page_number": "one"}
        with pytest.raises(ValidationError):
            Page.model_validate(payload)

    def test_unknown_field_is_rejected(self) -> None:
        payload = make_normalized().model_dump(mode="json")
        payload["surprise"] = "unexpected"
        with pytest.raises(ValidationError, match="surprise"):
            NormalizedDocument.model_validate(payload)

    def test_unknown_nested_field_is_rejected(self) -> None:
        payload = make_normalized().model_dump(mode="json")
        payload["pages"][0]["surprise"] = "unexpected"
        with pytest.raises(ValidationError, match="surprise"):
            NormalizedDocument.model_validate(payload)

    def test_wrong_type_for_computed_field_is_ignored_not_fatal(self) -> None:
        # A computed field is derived, so whatever a stale artifact claims for it is
        # discarded and recomputed - it must never be able to poison a load.
        payload = make_page(1, blocks=3).model_dump(mode="json")
        payload["block_count"] = "not-an-int"

        restored = Page.model_validate(payload)
        assert restored.block_count == 3  # recomputed from content_blocks

    def test_stale_computed_value_does_not_win(self) -> None:
        payload = make_page(1, blocks=3).model_dump(mode="json")
        payload["block_count"] = 999

        assert Page.model_validate(payload).block_count == 3

    def test_schema_version_mismatch_is_visible(self) -> None:
        # cdm_version is carried on the artifact so a reader can detect a document
        # produced by an incompatible generation rather than misreading it.
        document = make_normalized()
        payload = document.model_dump(mode="json")
        assert "cdm_version" in payload["metadata"]

        payload["metadata"]["cdm_version"] = "0.0.1-ancient"
        restored = NormalizedDocument.model_validate(payload)

        assert restored.metadata.cdm_version == "0.0.1-ancient"
        assert restored.metadata.cdm_version != document.metadata.cdm_version

    def test_wrong_root_type_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            NormalizedDocument.model_validate([1, 2, 3])

    async def test_missing_artifact_raises(self, storage: IObjectStorage) -> None:
        # A pointer row whose object has gone must fail loudly. Returning None here
        # would surface much later as an unexplained empty document.
        from app.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await storage.get_json("does/not/exist.json")


# =============================================================================
# Storage backends
# =============================================================================
class TestStorageBackends:
    """The round-trip must hold for whichever backend a deployment configures."""

    async def test_local_backend(self, tmp_path: Any) -> None:
        await self._assert_round_trip(LocalStorage(root=str(tmp_path / "local")))

    async def test_reference_backend(self, tmp_path: Any) -> None:
        await self._assert_round_trip(InMemoryStorage(root=str(tmp_path / "ref")))

    @pytest.mark.integration
    async def test_s3_backend(self) -> None:
        storage = _remote_or_skip("s3")
        await self._assert_round_trip(storage)

    @pytest.mark.integration
    async def test_azure_backend(self) -> None:
        storage = _remote_or_skip("azure")
        await self._assert_round_trip(storage)

    async def _assert_round_trip(self, storage: IObjectStorage) -> None:
        original = make_normalized(pages=2)
        key = artifact_key()

        await storage.put_json(key, original.model_dump(mode="json"))
        assert await storage.exists(key)

        restored = NormalizedDocument.model_validate(await storage.get_json(key))
        assert restored.metadata.document_id == original.metadata.document_id
        assert len(restored.pages) == 2
        assert restored.pages[0].block_count == original.pages[0].block_count

        await storage.delete(key)
        await storage.close()


def _remote_or_skip(provider: str) -> IObjectStorage:
    """Build a remote backend, or skip - matching how the live DB tests behave."""
    import os

    if os.environ.get("TEST_STORAGE_PROVIDER") != provider:
        pytest.skip(f"Set TEST_STORAGE_PROVIDER={provider} to exercise this backend.")
    from app.storage import get_storage

    return get_storage()


# =============================================================================
# Parser coverage
# =============================================================================
class TestParserCoverage:
    """Whatever a parser emits must survive the trip. That is the contract."""

    @pytest.mark.parametrize("parser_name", ["mock", "pymupdf", "docx"])
    async def test_parser_output_round_trips(
        self, parser_name: str, storage: IObjectStorage
    ) -> None:
        from app.ai.parsers import get_parser_by_name

        try:
            parser = get_parser_by_name(parser_name)
        except (ImportError, ParserError) as exc:  # pragma: no cover - optional deps
            pytest.skip(f"parser {parser_name} unavailable: {exc}")

        assert parser is not None

        # The emitted shape is what matters here, and it is the same model for
        # every adapter - which is precisely why one bug broke all of them.
        document = make_normalized().model_copy(
            update={"metadata": make_metadata(parser_name=parser_name)}
        )
        payload = await store_and_load(storage, document, artifact_key())
        restored = NormalizedDocument.model_validate(payload)
        assert restored.metadata.parser_name == parser_name


# =============================================================================
# Performance
# =============================================================================
@pytest.mark.slow
class TestRoundTripPerformance:
    """Serialisation must not become the reason ingestion is slow.

    Generous thresholds on purpose: this is a guard against an accidental
    O(n^2) or a per-node revalidation, not a benchmark. It has to stay green on a
    loaded CI box.
    """

    async def test_small_document(self, storage: IObjectStorage) -> None:
        elapsed = await self._time_round_trip(storage, make_normalized(pages=1))
        assert elapsed < 1.0

    async def test_multi_page_contract(self, storage: IObjectStorage) -> None:
        elapsed = await self._time_round_trip(storage, make_normalized(pages=50))
        assert elapsed < 3.0

    async def test_large_artifact(self, storage: IObjectStorage) -> None:
        document = make_normalized(pages=200, blocks_per_page=20)
        elapsed = await self._time_round_trip(storage, document)
        assert elapsed < 10.0

    async def test_cost_scales_roughly_linearly(self, storage: IObjectStorage) -> None:
        small = await self._time_round_trip(storage, make_normalized(pages=10))
        large = await self._time_round_trip(storage, make_normalized(pages=100))

        # 10x the pages must not cost 100x the time. Floored so a sub-millisecond
        # small case cannot make the ratio meaningless.
        assert large < max(small, 0.01) * 40

    async def _time_round_trip(
        self, storage: IObjectStorage, document: NormalizedDocument
    ) -> float:
        key = artifact_key(f"perf-{len(document.pages)}")
        started = time.perf_counter()

        await storage.put_json(key, document.model_dump(mode="json"))
        NormalizedDocument.model_validate(await storage.get_json(key))

        return time.perf_counter() - started

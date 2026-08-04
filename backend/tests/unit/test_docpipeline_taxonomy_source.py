"""The clause taxonomy, now sourced from the platform's own tables.

`cip_docMapping` was owned by another team and existed only on a database that has
been decommissioned. Because `DocPipelineStage` reads it before anything else, and
extraction depends on that stage, its absence meant *no document could be
processed at all* - the failure surfaced as a generic "An unexpected error
occurred" after three retries.

`mapping.py` now reads `document_profiles` and the Clause Master instead. These
tests pin the three properties that make that substitution safe rather than merely
convenient:

* the label set is `AgreementType` values, so a classification is directly usable
  as `contracts.agreement_type` with no translation step;
* the fallback bucket is always offered, so the classifier can say "I could not
  tell" instead of being forced to pick;
* an unclassifiable document gets the *whole* Clause Master rather than nothing,
  because it is the document most worth looking hard at.

Sessions are stubs. What is being asserted is the selection logic, not SQLAlchemy.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.ai.docpipeline import mapping, taxonomy
from app.core.enums import AgreementType


# =============================================================================
# Doubles
# =============================================================================
class Row:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class StubResult:
    def __init__(self, rows: list[Any], *, scalar: bool = False) -> None:
        self._rows = rows
        self._scalar = scalar

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> StubResult:
        return StubResult(self._rows, scalar=True)

    def first(self) -> Any:
        return self._rows[0] if self._rows else None


class ScriptedSession:
    """Returns the next canned result per `execute`, and records the count."""

    def __init__(self, *results: list[Any], scalar: Any = 0) -> None:
        self._results = list(results)
        self._scalar_value = scalar
        self.executes = 0

    async def execute(self, _statement: Any) -> StubResult:
        rows = self._results[self.executes] if self.executes < len(self._results) else []
        self.executes += 1
        return StubResult(rows)

    async def scalar(self, _statement: Any) -> Any:
        """`_clause_keys_for` counts mappings to tell "none configured" from "all off"."""
        return self._scalar_value


def profile(agreement_type: str, **overrides: Any) -> Row:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "agreement_type": agreement_type,
        "mandatory_clauses": [],
        "optional_clauses": [],
    }
    return Row(**{**base, **overrides})


def category(key: str, name: str, *, synonyms: list[str] | None = None) -> Row:
    rule = Row(is_active=True, synonyms=synonyms or [])
    return Row(key=key, name=name, description=f"{name} clause.", rules=[rule])


# =============================================================================
# The label set
# =============================================================================
@pytest.mark.asyncio
class TestLoadDocTypes:
    async def test_labels_come_from_the_configured_profiles(self) -> None:
        db = ScriptedSession([("msa",), ("nda",), ("lease",)])

        types = await mapping.load_doc_types(db)  # type: ignore[arg-type]

        assert types[:3] == ["msa", "nda", "lease"]

    async def test_the_fallback_bucket_is_always_offered(self) -> None:
        """Without it the classifier cannot answer "I could not tell"."""
        db = ScriptedSession([("msa",), ("nda",)])

        types = await mapping.load_doc_types(db)  # type: ignore[arg-type]

        assert mapping.FALLBACK_DOC_TYPE in types

    async def test_the_fallback_is_not_duplicated(self) -> None:
        db = ScriptedSession([("msa",), (mapping.FALLBACK_DOC_TYPE,)])

        types = await mapping.load_doc_types(db)  # type: ignore[arg-type]

        assert types.count(mapping.FALLBACK_DOC_TYPE) == 1

    async def test_no_profiles_is_a_lookup_error(self) -> None:
        """The one failure `copilot._document_type` is allowed to absorb."""
        db = ScriptedSession([])

        with pytest.raises(LookupError):
            await mapping.load_doc_types(db)  # type: ignore[arg-type]

    async def test_every_label_is_an_agreement_type(self) -> None:
        """The label is written straight to `contracts.agreement_type`."""
        values = {member.value for member in AgreementType}
        db = ScriptedSession([("msa",), ("license_agreement",), ("purchase_order",)])

        types = await mapping.load_doc_types(db)  # type: ignore[arg-type]

        assert set(types) <= values


# =============================================================================
# The clause list
# =============================================================================
@pytest.mark.asyncio
class TestLoadClauses:
    """The clause list comes from `agreement_type_clauses`.

    The same table the extraction stage reads, so the clauses the detector looks
    for and the clauses the engine extracts cannot drift apart. Each scripted
    session supplies, in order: the active mapping rows, then the categories.
    """

    async def test_clauses_come_from_the_active_mapping(self) -> None:
        db = ScriptedSession(
            [("confidentiality",), ("term",)],
            [
                category("confidentiality", "Confidentiality / NDA"),
                category("term", "Term / Duration"),
            ],
        )

        specs = await mapping.load_clauses(db, "nda")  # type: ignore[arg-type]

        assert [spec.clause for spec in specs] == ["Confidentiality / NDA", "Term / Duration"]

    async def test_the_clause_name_is_the_clause_master_name(self) -> None:
        """The retired taxonomy held exactly these strings - the swap is lossless."""
        db = ScriptedSession(
            [("intellectual_property",)],
            [category("intellectual_property", "IP Ownership")],
        )

        specs = await mapping.load_clauses(db, "msa")  # type: ignore[arg-type]

        assert specs[0].clause == "IP Ownership"

    async def test_synonyms_become_the_prompt_description(self) -> None:
        """Better prompt material than the boilerplate `description` column."""
        db = ScriptedSession(
            [("limitation_of_liability",)],
            [
                category(
                    "limitation_of_liability",
                    "Limitation of Liability",
                    synonyms=["Liability Cap", "Limitation on Damages"],
                )
            ],
        )

        specs = await mapping.load_clauses(db, "msa")  # type: ignore[arg-type]

        assert specs[0].description == "also called Liability Cap, Limitation on Damages"
        assert specs[0].as_prompt_line().startswith("- Limitation of Liability: also called")

    async def test_the_fallback_type_gets_the_whole_clause_master(self) -> None:
        """An unclassified document is the one worth looking hardest at."""
        db = ScriptedSession(
            [
                category("confidentiality", "Confidentiality / NDA"),
                category("term", "Term / Duration"),
            ]
        )

        specs = await mapping.load_clauses(db, mapping.FALLBACK_DOC_TYPE)  # type: ignore[arg-type]

        # One query only - the mapping lookup is skipped entirely.
        assert db.executes == 1
        assert len(specs) == 2

    async def test_an_unconfigured_type_gets_the_whole_clause_master(self) -> None:
        """No mapping rows at all: nobody has configured this type."""
        db = ScriptedSession([], [category("term", "Term / Duration")], scalar=0)

        specs = await mapping.load_clauses(db, "sow")  # type: ignore[arg-type]

        assert [spec.clause for spec in specs] == ["Term / Duration"]

    async def test_every_clause_switched_off_is_not_the_same_as_unconfigured(self) -> None:
        """Configured and all inactive is a deliberate state, not "run everything".

        This is the distinction the mapping table exists for. With the old JSONB
        arrays there was nowhere to say it, so switching a clause off and deleting
        it were the same edit, and an empty list was indistinguishable from a type
        nobody had configured.

        Asserted on `_clause_keys_for` rather than on `load_clauses`, because the
        difference is expressed as `WHERE key IN ()` - real SQL returns nothing,
        but a stub session hands back its canned rows whatever the predicate says.
        Testing the end result here would assert the stub, not the code.
        """
        configured_all_off = ScriptedSession([], scalar=5)
        never_configured = ScriptedSession([], scalar=0)

        assert await mapping._clause_keys_for(configured_all_off, "msa") == set()  # type: ignore[arg-type]
        assert await mapping._clause_keys_for(never_configured, "msa") is None  # type: ignore[arg-type]

    async def test_a_category_with_no_name_is_skipped(self) -> None:
        db = ScriptedSession(
            [("term",), ("blank",)],
            [category("term", "Term / Duration"), category("blank", "   ")],
        )

        specs = await mapping.load_clauses(db, "msa")  # type: ignore[arg-type]

        assert [spec.clause for spec in specs] == ["Term / Duration"]


# =============================================================================
# Resolution, including the retired vocabulary
# =============================================================================
@pytest.mark.asyncio
class TestResolveDocumentType:
    async def test_the_fallback_is_never_a_filter(self) -> None:
        """Narrowing to "unknown" would exclude every document that *was* classified."""
        assert await mapping.resolve_document_type(None, mapping.FALLBACK_DOC_TYPE) is None  # type: ignore[arg-type]

    async def test_the_retired_fallback_spelling_is_also_not_a_filter(self) -> None:
        """Documents were filed under "Others" before the vocabulary changed."""
        assert await mapping.resolve_document_type(None, "Others") is None  # type: ignore[arg-type]

    async def test_an_empty_label_resolves_to_nothing(self) -> None:
        assert await mapping.resolve_document_type(None, "") is None  # type: ignore[arg-type]

    async def test_a_configured_type_resolves_to_itself(self) -> None:
        db = ScriptedSession([("msa",), ("nda",)])

        resolved = await mapping.resolve_document_type(db, "nda")  # type: ignore[arg-type]

        assert resolved is not None
        assert resolved.label == "nda"
        assert resolved.agreement_type == AgreementType.NDA.value

    async def test_case_and_punctuation_do_not_decide(self) -> None:
        db = ScriptedSession([("license_agreement",)])

        resolved = await mapping.resolve_document_type(db, "License Agreement")  # type: ignore[arg-type]

        assert resolved is not None
        assert resolved.agreement_type == AgreementType.LICENSE_AGREEMENT.value

    async def test_a_retired_label_still_finds_its_documents(self) -> None:
        """"Contract cum Order Form" reads better in a question than `purchase_order`."""
        db = ScriptedSession([("purchase_order",), ("msa",)])

        resolved = await mapping.resolve_document_type(db, "Contract cum Order Form")  # type: ignore[arg-type]

        assert resolved is not None
        assert resolved.agreement_type == AgreementType.PURCHASE_ORDER.value

    async def test_an_unknown_label_is_none_not_the_nearest_guess(self) -> None:
        db = ScriptedSession([("msa",), ("nda",)])

        assert await mapping.resolve_document_type(db, "Vendor MSA") is None  # type: ignore[arg-type]

    async def test_no_profiles_does_not_fail_the_question(self) -> None:
        db = ScriptedSession([])

        assert await mapping.resolve_document_type(db, "msa") is None  # type: ignore[arg-type]


# =============================================================================
# The translation layer
# =============================================================================
class TestAgreementTypeFor:
    def test_an_agreement_type_maps_to_itself(self) -> None:
        assert taxonomy.agreement_type_for("lease") == (AgreementType.LEASE.value, None)

    def test_a_retired_label_still_translates(self) -> None:
        assert taxonomy.agreement_type_for("Contract cum Order Form") == (
            AgreementType.PURCHASE_ORDER.value,
            "contract_cum_order_form",
        )

    def test_an_unknown_label_is_filed_as_other_not_invented(self) -> None:
        """An unknown value in `agreement_type` is a filter bucket nothing matches."""
        agreement_type, subtype = taxonomy.agreement_type_for("Widget Purchase Deed")

        assert agreement_type == AgreementType.OTHER.value
        assert subtype == "widget_purchase_deed"

    def test_every_configured_profile_type_round_trips(self) -> None:
        """The ingest side and the query side must agree on every label."""
        for member in AgreementType:
            assert taxonomy.agreement_type_for(member.value) == (member.value, None)

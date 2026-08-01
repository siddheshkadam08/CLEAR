"""The golden dataset: what a question is expected to retrieve and answer.

Two design decisions here are worth stating, because both were tempting to get
wrong.

**Expectations are partial by design.** A case may name only the contracts that
should be retrieved, or only the clause ids, or only the pages. Writing full
expectations for a thousand questions is work nobody will do, so a dataset where
each case names *whatever the author knew* is the one that actually gets built.
The relevance judgement (:mod:`app.evaluation.metrics.relevance`) is written to
degrade cleanly against partial expectations rather than to demand complete ones.

**``should_answer`` is a first-class field, and half the dataset should set it to
false.** A retrieval benchmark that only contains answerable questions measures
recall and nothing else - it cannot tell you whether the guardrail fires when it
should, which is the property that stops the platform inventing contract terms.
Negative cases are not an afterthought; they are how the guardrail is scored.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

#: Dataset schema version. Bumped when the *shape* of a case changes in a way
#: that older readers cannot interpret, so a stale file fails loudly on load
#: rather than being silently half-read.
SCHEMA_VERSION = 1


def _uuid_list(values: Any) -> list[uuid.UUID]:
    """Coerce a JSON list into UUIDs, rejecting anything malformed.

    Strict rather than lenient: a mistyped id that silently becomes "no
    expectation" would make a case pass for the wrong reason, and a benchmark
    that passes wrongly is worse than one that fails loudly.
    """
    parsed: list[uuid.UUID] = []
    for value in values or []:
        if isinstance(value, uuid.UUID):
            parsed.append(value)
            continue
        try:
            parsed.append(uuid.UUID(str(value)))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"'{value}' is not a valid id") from exc
    return parsed


def _str_list(values: Any) -> list[str]:
    return [str(value).strip() for value in (values or []) if str(value).strip()]


def _int_list(values: Any) -> list[int]:
    parsed: list[int] = []
    for value in values or []:
        try:
            parsed.append(int(value))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"'{value}' is not a page number") from exc
    return parsed


@dataclass(slots=True)
class GoldenExpectation:
    """What a correct answer to one question would have drawn on.

    Every collection is optional. An empty one means "no expectation on this
    dimension", **not** "expected to be empty" - the difference matters, because
    scoring the second interpretation would fail every case that only named its
    contracts.
    """

    #: Contracts that must appear among the retrieved evidence.
    contracts: list[uuid.UUID] = field(default_factory=list)
    #: Clause or chunk ids that must be retrieved. The most precise expectation
    #: available, and the only one that scores position accurately.
    clauses: list[uuid.UUID] = field(default_factory=list)
    #: Pages the answer should be drawn from. Used with ``contracts``: a page
    #: number alone identifies nothing.
    pages: list[int] = field(default_factory=list)
    #: Section headings, matched case- and punctuation-insensitively.
    headings: list[str] = field(default_factory=list)
    #: Agreement types the search should have been narrowed to, if any.
    agreement_types: list[str] = field(default_factory=list)
    #: False for a question the corpus genuinely cannot answer. These score the
    #: guardrail; a dataset without them scores nothing about hallucination.
    should_answer: bool = True
    #: The planner's expected intent, for planner accuracy. Optional because it
    #: is an internal classification most dataset authors will not label.
    intent: str | None = None
    #: The document type the classifier should identify, when the question
    #: genuinely indicates one.
    document_type: str | None = None
    #: Substrings the answer must contain. A blunt but effective grounding check
    #: for cases with a single unambiguous correct value ("thirty days").
    answer_contains: list[str] = field(default_factory=list)
    #: Substrings the answer must NOT contain. Catches a known hallucination -
    #: a figure from a neighbouring contract, a term the model likes to invent.
    answer_excludes: list[str] = field(default_factory=list)

    @property
    def has_relevance_signal(self) -> bool:
        """True when this case can score retrieval at all.

        A case with ``should_answer=false`` and no expectations is still valid -
        it scores the guardrail - but it must not be counted in a recall
        denominator, or the recall figure silently becomes a function of how many
        negative cases the dataset happens to contain.
        """
        return bool(self.contracts or self.clauses or self.headings)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GoldenExpectation:
        return cls(
            contracts=_uuid_list(payload.get("contracts")),
            clauses=_uuid_list(payload.get("clauses")),
            pages=_int_list(payload.get("pages")),
            headings=_str_list(payload.get("headings")),
            agreement_types=_str_list(
                payload.get("agreementTypes") or payload.get("agreement_types")
            ),
            should_answer=bool(payload.get("shouldAnswer", payload.get("should_answer", True))),
            intent=(payload.get("intent") or None),
            document_type=(payload.get("documentType") or payload.get("document_type") or None),
            answer_contains=_str_list(
                payload.get("answerContains") or payload.get("answer_contains")
            ),
            answer_excludes=_str_list(
                payload.get("answerExcludes") or payload.get("answer_excludes")
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "contracts": [str(value) for value in self.contracts],
            "clauses": [str(value) for value in self.clauses],
            "pages": list(self.pages),
            "headings": list(self.headings),
            "agreementTypes": list(self.agreement_types),
            "shouldAnswer": self.should_answer,
            "intent": self.intent,
            "documentType": self.document_type,
            "answerContains": list(self.answer_contains),
            "answerExcludes": list(self.answer_excludes),
        }


@dataclass(slots=True)
class GoldenCase:
    """One benchmark question."""

    id: str
    question: str
    project_id: uuid.UUID | None = None
    #: Narrow to one contract, mirroring the ``contractId`` a caller may send.
    contract_id: uuid.UUID | None = None
    expected: GoldenExpectation = field(default_factory=GoldenExpectation)
    #: Free-form labels: ``nda``, ``msa``, ``banking``, ``multilingual``,
    #: ``large-document``, ``adversarial``. Used to slice a report by segment,
    #: which is how a regression in one contract family is spotted before it is
    #: averaged away across the whole set.
    tags: list[str] = field(default_factory=list)
    #: Why this case exists. Read by whoever has to fix it when it fails.
    notes: str = ""

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> GoldenCase:
        case_id = str(payload.get("id") or "").strip()
        question = str(payload.get("question") or "").strip()
        if not case_id:
            raise ValueError("every case needs an 'id'")
        if not question:
            raise ValueError(f"case '{case_id}' has no question")

        project_raw = payload.get("projectId") or payload.get("project_id")
        contract_raw = payload.get("contractId") or payload.get("contract_id")

        return cls(
            id=case_id,
            question=question,
            project_id=_uuid_list([project_raw])[0] if project_raw else None,
            contract_id=_uuid_list([contract_raw])[0] if contract_raw else None,
            expected=GoldenExpectation.from_dict(payload.get("expected") or {}),
            tags=_str_list(payload.get("tags")),
            notes=str(payload.get("notes") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "projectId": str(self.project_id) if self.project_id else None,
            "contractId": str(self.contract_id) if self.contract_id else None,
            "expected": self.expected.as_dict(),
            "tags": list(self.tags),
            "notes": self.notes,
        }


@dataclass(slots=True)
class GoldenDataset:
    """A named, versioned set of cases."""

    name: str
    version: str = "1"
    description: str = ""
    schema_version: int = SCHEMA_VERSION
    cases: list[GoldenCase] = field(default_factory=list)
    #: Dataset-level tags, in addition to per-case ones.
    tags: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Iterator[GoldenCase]:
        return iter(self.cases)

    @property
    def identifier(self) -> str:
        return f"{self.name}@{self.version}"

    def filter(
        self,
        *,
        tags: list[str] | None = None,
        project_id: uuid.UUID | None = None,
        limit: int | None = None,
    ) -> GoldenDataset:
        """A subset, as a dataset in its own right.

        Tag matching is **any-of**, not all-of: a run tagged ``nda,msa`` means
        "the NDA and MSA segments", which is what someone slicing a report is
        asking for.
        """
        cases = self.cases
        if tags:
            wanted = {tag.lower() for tag in tags}
            cases = [
                case
                for case in cases
                if wanted & ({tag.lower() for tag in case.tags} | {t.lower() for t in self.tags})
            ]
        if project_id is not None:
            cases = [case for case in cases if case.project_id == project_id]
        if limit is not None:
            cases = cases[:limit]

        return GoldenDataset(
            name=self.name,
            version=self.version,
            description=self.description,
            schema_version=self.schema_version,
            cases=list(cases),
            tags=list(self.tags),
        )

    def tag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.cases:
            for tag in case.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return dict(sorted(counts.items()))

    def statistics(self) -> dict[str, Any]:
        answerable = sum(1 for case in self.cases if case.expected.should_answer)
        return {
            "name": self.name,
            "version": self.version,
            "cases": len(self.cases),
            "answerable": answerable,
            "unanswerable": len(self.cases) - answerable,
            "with_relevance_signal": sum(
                1 for case in self.cases if case.expected.has_relevance_signal
            ),
            "tags": self.tag_counts(),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "schemaVersion": self.schema_version,
            "tags": list(self.tags),
            "cases": [case.as_dict() for case in self.cases],
        }


__all__ = [
    "SCHEMA_VERSION",
    "GoldenCase",
    "GoldenDataset",
    "GoldenExpectation",
]

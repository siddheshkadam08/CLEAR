"""Stage 1: what kind of document is this?

Only the first few pages are read. A contract announces itself in its title,
recitals and definitions; by page six it is reciting obligations that look much
the same whatever the instrument. Reading further costs tokens and adds noise.

The label set is not hardcoded here - it is whatever document profiles are
configured, read at runtime by :func:`app.ai.docpipeline.mapping.load_doc_types`.
That label is the lookup key for the clause list that follows *and* the value
written to ``contracts.agreement_type``, so a classifier emitting anything else
would produce a document type with no clauses to look for and no filter to match.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.ai.docpipeline import taxonomy
from app.ai.docpipeline.inference import call_structured
from app.ai.docpipeline.mapping import FALLBACK_DOC_TYPE
from app.ai.docpipeline.source import PageContent
from app.ai.rag.providers import IInferenceProvider, get_inference_provider
from app.ai.routing import LLMTask
from app.core.enums import AgreementType
from app.core.logging import get_logger

logger = get_logger(__name__)

#: How many pages the classifier reads. Five is enough to see title, parties,
#: recitals and the start of the definitions.
CLASSIFICATION_PAGE_WINDOW = 5

#: What distinguishes each document type. Keyed on ``AgreementType`` values, which
#: is what ``document_profiles.agreement_type`` holds and therefore what
#: :func:`app.ai.docpipeline.mapping.load_doc_types` returns.
#:
#: A configured type absent from here still gets classified - it goes to the model
#: on its name alone (see ``_system_prompt``). The hints are hand-written because
#: the seeded ``document_profiles.description`` is boilerplate ("Processing profile
#: for Lease Agreement.") and tells a model nothing it cannot read off the label.
_TYPE_HINTS: dict[str, str] = {
    AgreementType.MSA.value: (
        "Master Service Agreement. A framework of terms governing future work: "
        "no specific deliverable, quantity or price on its face, and it "
        "anticipates separate statements of work or order forms."
    ),
    AgreementType.LICENSE_AGREEMENT.value: (
        "Grants a right to use software, technology or other intellectual "
        "property. Look for a grant of licence, scope or field of use, "
        "exclusivity, and restrictions on copying or sublicensing."
    ),
    AgreementType.PURCHASE_ORDER.value: (
        "Agreement terms bundled together with a concrete order on the face of "
        "the document: line items, quantities, unit prices, subscription term "
        "or effective dates filled in for this specific purchase."
    ),
    AgreementType.AMENDMENT.value: (
        "Amends, supplements or extends a previously executed agreement, which "
        "it names and dates. Short, and meaningless without the parent contract."
    ),
    AgreementType.NDA.value: (
        "Confidentiality is the substance of the agreement, not one clause of "
        "it: definition of confidential information, permitted use, duration of "
        "the obligation, return or destruction on termination."
    ),
    AgreementType.VENDOR_AGREEMENT.value: (
        "A supplier provides goods or defined services on recurring terms. "
        "Distinguished from an MSA by naming what is supplied rather than "
        "deferring it to a future statement of work."
    ),
    AgreementType.EMPLOYMENT_AGREEMENT.value: (
        "Between an employer and one named individual: role, salary, benefits, "
        "notice period, and usually restrictive covenants. The counterparty is a "
        "person, not a company."
    ),
    AgreementType.CONSULTING_AGREEMENT.value: (
        "An independent contractor supplies professional services. Look for a "
        "statement that the supplier is not an employee, and for deliverables or "
        "rates rather than a salary."
    ),
    AgreementType.LEASE.value: (
        "Grants possession of property - premises, land or equipment - for a term "
        "in exchange for rent. Look for a described premises, rent and a term."
    ),
    AgreementType.INSURANCE_POLICY.value: (
        "An insurer accepts a defined risk for a premium: coverage limits, "
        "exclusions, deductibles, and a claims procedure."
    ),
    AgreementType.GOVERNMENT_CONTRACT.value: (
        "One party is a public body. Look for procurement references, statutory "
        "flow-down clauses and compliance obligations a commercial contract "
        "would not carry."
    ),
    AgreementType.HEALTHCARE_AGREEMENT.value: (
        "Concerns clinical services, patient data or medical supply. Look for "
        "protected health information, HIPAA or equivalent, and clinical "
        "responsibilities."
    ),
    AgreementType.RESEARCH_COLLABORATION.value: (
        "Two or more parties jointly conduct research. Look for a work plan, "
        "allocation of foreground and background IP, and publication rights."
    ),
    FALLBACK_DOC_TYPE: (
        "Use this when the document does not clearly match any other type, "
        "including when it is not a contract at all."
    ),
}


@dataclass(frozen=True, slots=True)
class DocumentClassification:
    """The verdict, with enough provenance to explain it."""

    doc_type: str
    confidence: float
    reason: str
    model: str
    pages_used: int
    duration_ms: int
    fell_back: bool = False


class DocumentTypeClassifier:
    """Classifies a document into one of the types ``cip_docMapping`` knows."""

    def __init__(self, provider: IInferenceProvider | None = None) -> None:
        self._provider = provider or get_inference_provider()

    async def classify(
        self,
        pages: Sequence[PageContent],
        doc_types: Sequence[str],
        *,
        page_window: int = CLASSIFICATION_PAGE_WINDOW,
    ) -> DocumentClassification:
        if not doc_types:
            raise ValueError("No document types to classify into.")

        window = list(pages[:page_window])
        if not window:
            raise ValueError("No pages to classify.")

        started = time.perf_counter()
        result = await call_structured(
            self._provider,
            system=_system_prompt(doc_types),
            prompt=_user_prompt(window, len(pages)),
            schema=_schema(doc_types),
            task=LLMTask.DOCUMENT_CLASSIFICATION,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        raw = str(result.data.get("document_type") or "").strip()
        doc_type, fell_back = _normalise(raw, doc_types)

        classification = DocumentClassification(
            doc_type=doc_type,
            confidence=_confidence(result.data.get("confidence")),
            reason=str(result.data.get("reason") or "").strip()[:600],
            model=result.model,
            pages_used=len(window),
            duration_ms=duration_ms,
            fell_back=fell_back,
        )
        logger.info(
            "docpipeline_document_classified",
            doc_type=classification.doc_type,
            confidence=classification.confidence,
            model=classification.model,
            pages_used=classification.pages_used,
            duration_ms=duration_ms,
            fell_back=fell_back,
            raw_label=raw if fell_back else None,
        )
        return classification


def _normalise(raw: str, doc_types: Sequence[str]) -> tuple[str, bool]:
    """Map the model's answer onto the taxonomy, or onto the fallback.

    Returns ``(doc_type, fell_back)``. Anything unrecognised becomes the fallback
    type rather than being passed through: an invented label would join to zero
    clauses and the run would report "0 clauses found" for what is really a
    classification miss.
    """
    for candidate in doc_types:
        if raw.casefold() == candidate.casefold():
            return candidate, False

    return _fallback_label(doc_types), True


def _fallback_label(doc_types: Sequence[str]) -> str:
    """The label set's own "I could not tell" bucket.

    Matched by meaning rather than by string, so a caller passing the retired
    ``"Others"`` spelling still gets its own bucket back rather than a different
    document type.

    Returning ``FALLBACK_DOC_TYPE`` when the set contains no such bucket is
    deliberate. This used to return ``doc_types[0]`` - the *first* type,
    alphabetically - which turned "none of these fit" into a confident,
    arbitrary answer, and the caller had only `fell_back` to tell them apart.
    """
    for candidate in doc_types:
        if candidate.casefold() == FALLBACK_DOC_TYPE.casefold():
            return candidate

    # Retired vocabularies spelled it "Others" and mapped it to AgreementType.OTHER.
    for candidate in doc_types:
        legacy = taxonomy.legacy_agreement_type(candidate)
        if legacy is not None and legacy[0] == AgreementType.OTHER.value:
            return candidate

    return FALLBACK_DOC_TYPE


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _system_prompt(doc_types: Sequence[str]) -> str:
    lines = [
        "You classify commercial legal documents into exactly one type.",
        "",
        "The permitted types, and what distinguishes each:",
    ]
    for doc_type in doc_types:
        hint = _TYPE_HINTS.get(doc_type)
        lines.append(f"- {doc_type}: {hint}" if hint else f"- {doc_type}")
    lines += [
        "",
        "Rules:",
        "- Answer with one of the type names above, copied exactly.",
        f"- If no type clearly fits, answer '{FALLBACK_DOC_TYPE}'. Do not guess "
        "between two types that both fit poorly.",
        "- Judge the document by what it does, not by words that appear in it. "
        "An MSA contains a confidentiality clause; that does not make it an NDA.",
        "- Give the confidence you actually have. A cover page and a stamp are weak evidence.",
    ]
    return "\n".join(lines)


def _user_prompt(window: Sequence[PageContent], total_pages: int) -> str:
    parts = [
        f"The first {len(window)} pages of a {total_pages}-page document, "
        "paragraph by paragraph in reading order.",
        "",
    ]
    for page in window:
        parts.append(f"--- PAGE {page.page_number} ---")
        for paragraph in page.paragraphs:
            role = f" ({paragraph.role})" if paragraph.role else ""
            parts.append(f"[{paragraph.ref}]{role} {paragraph.content}")
        parts.append("")
    parts.append("What type of document is this?")
    return "\n".join(parts)


def _schema(doc_types: Sequence[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": list(doc_types),
                "description": "Exactly one of the permitted type names.",
            },
            "confidence": {
                "type": "number",
                "description": "0.0 to 1.0.",
            },
            "reason": {
                "type": "string",
                "description": "One or two sentences citing what decided it.",
            },
        },
        "required": ["document_type", "confidence", "reason"],
        "additionalProperties": False,
    }

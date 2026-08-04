"""Bridging the two clause vocabularies.

`cip_docMapping` names clauses the way a lawyer writes them - "Confidentiality/NDA",
"Term/duration" - and the Clause Master keys them the way code does -
``confidentiality``, ``term``. Both are legitimate; they were written by different
people for different readers, and neither is going to change to suit the other.

Twelve of the twenty-three names match once punctuation and case are normalised.
The remaining eleven are stated below, because guessing them is exactly the kind
of thing that fails silently: an unmapped clause does not raise, it just quietly
gets no attributes, and a dashboard tile reads empty for a reason nobody can see.

Unmapped is still a legitimate outcome. Clause Master carries seven categories
`cip_docMapping` has no equivalent for (``scope_of_work``, ``data_protection``,
``compliance``, ``service_level``, ``publicity``, ``definitions``,
``entire_agreement``), and a taxonomy edit could add a clause on either side at
any time. A clause with no counterpart keeps the text and geometry the document
pipeline stored; it simply gets no typed attributes.
"""

from __future__ import annotations

import re

from app.core.enums import AgreementType
from app.core.logging import get_logger

logger = get_logger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

#: cip_docMapping clause name -> Clause Master key, for the pairs that
#: normalisation cannot reach. Verified against both tables rather than inferred
#: from the names: four of these are not what the wording suggests -
#: "IP ownership" is ``intellectual_property`` and not ``ip_ownership``,
#: "Renewal" is ``auto_renewal``, "Non-solicit" is ``non_solicitation``, and
#: "Notice requirements" is ``notice`` singular.
EXPLICIT: dict[str, str] = {
    "assignment change of control": "assignment",
    "confidentiality nda": "confidentiality",
    "ip ownership": "intellectual_property",
    "insurance requirements": "insurance",
    "license grants": "license_grant",
    "non solicit": "non_solicitation",
    "notice requirements": "notice",
    "renewal": "auto_renewal",
    "surviving clauses": "survival",
    "term duration": "term",
    "warranties representations": "warranty",
}


#: ``cip_docMapping`` document type -> ``(AgreementType, agreement_subtype)``.
#:
#: The two vocabularies were written for different readers, same as the clause
#: names above. Five of the six line up; the sixth does not, and the disagreement
#: is worth stating rather than smoothing over:
#:
#: "Contract cum Order Form" is agreement terms bundled with a concrete order,
#: and ``AgreementType`` has no member for that. It maps to ``PURCHASE_ORDER``
#: as the closest instrument, with the exact label kept in ``agreement_subtype``.
#: The reason it is not stored verbatim in ``agreement_type``: the Dashboard's
#: type distribution and the Contracts filter panel both bucket on that column,
#: so an off-taxonomy value there becomes a bucket nobody can filter to.
#: ``agreement_subtype`` is aggregated by nothing, so the fidelity survives.
DOC_TYPE_AGREEMENTS: dict[str, tuple[str, str | None]] = {
    "msa": (AgreementType.MSA.value, None),
    "nda": (AgreementType.NDA.value, None),
    "license agreement": (AgreementType.LICENSE_AGREEMENT.value, None),
    "addendum": (AgreementType.AMENDMENT.value, "addendum"),
    "contract cum order form": (
        AgreementType.PURCHASE_ORDER.value,
        "contract_cum_order_form",
    ),
    "others": (AgreementType.OTHER.value, None),
}


#: Every ``AgreementType`` value, for the identity check below. Built once.
_AGREEMENT_TYPE_VALUES: frozenset[str] = frozenset(member.value for member in AgreementType)


def legacy_agreement_type(doc_type: str) -> tuple[str, str | None] | None:
    """A retired ``cip_docMapping`` label as ``(agreement_type, subtype)``.

    ``None`` when the label is not one of the six. Kept separate from
    :func:`agreement_type_for` so a caller can tell "this is an old label I
    translated" from "I gave up and filed it as other" - the first is a match, the
    second is not.
    """
    return DOC_TYPE_AGREEMENTS.get(normalise(doc_type))


def agreement_type_for(doc_type: str) -> tuple[str, str | None]:
    """A document type as ``(agreement_type, subtype)``.

    Document types are now ``AgreementType`` values taken from the configured
    document profiles, so the common case is identity - the label the classifier
    answers with *is* the value written to ``contracts.agreement_type``.

    The six retired ``cip_docMapping`` labels are still translated, because
    documents classified under them are still in the database and a question asked
    in those words should still find them.

    Anything else becomes ``other`` with the raw label preserved in the subtype,
    and says so in the log. The alternative is writing an unknown label into
    ``agreement_type``, where it would silently create a filter bucket matching
    nothing the UI offers.
    """
    candidate = (doc_type or "").strip()
    if candidate in _AGREEMENT_TYPE_VALUES:
        return candidate, None

    mapped = legacy_agreement_type(candidate)
    if mapped is not None:
        return mapped

    key = normalise(candidate)
    logger.info(
        "doc_type_unmapped",
        doc_type=doc_type,
        normalised=key,
        reason="not an AgreementType and not a retired label; filed as 'other'",
    )
    return AgreementType.OTHER.value, key.replace(" ", "_")[:64] or None


def normalise(name: str) -> str:
    """Clause name reduced to comparable form.

    ``btrim`` is not enough on its own: ten ``Others`` rows carry a trailing
    space, which no document type duplicates today but which breaks an exact
    join and would drop those categories without a word.
    """
    return _NON_ALNUM.sub(" ", name.strip().lower()).strip()


def clause_master_key(clause: str, *, known_keys: set[str] | None = None) -> str | None:
    """The Clause Master key for a ``cip_docMapping`` clause name, or ``None``.

    ``known_keys`` is the set actually present in the database. Passing it turns
    a mapping that has drifted - a renamed key, a category an administrator
    deleted - into ``None`` plus a log line, rather than a definition lookup that
    fails later with nothing pointing back here.
    """
    key = normalise(clause)
    candidate = EXPLICIT.get(key) or _NON_ALNUM.sub("_", key).strip("_")

    if known_keys is not None and candidate not in known_keys:
        logger.info(
            "clause_master_key_unmapped",
            clause=clause,
            candidate=candidate,
            reason="no such key in the Clause Master",
        )
        return None
    return candidate


def map_clauses(
    clauses: list[str], *, known_keys: set[str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Split clause names into ``{name: key}`` and the ones with no counterpart.

    Returning the unmapped list rather than dropping it is the point: it is the
    difference between "this contract has no liability cap" and "nothing looked
    for one", and only the caller can tell the user which.
    """
    mapped: dict[str, str] = {}
    unmapped: list[str] = []

    for clause in clauses:
        key = clause_master_key(clause, known_keys=known_keys)
        if key is None:
            unmapped.append(clause)
        else:
            mapped[clause] = key

    return mapped, unmapped


__all__ = [
    "DOC_TYPE_AGREEMENTS",
    "EXPLICIT",
    "agreement_type_for",
    "clause_master_key",
    "map_clauses",
    "normalise",
]

"""JSON Schemas for every extraction call (§13).

Free-form model output is prohibited. Each call declares a schema, the provider
enforces it, and the validator then applies the rules a schema cannot express
(coherence between fields, quotes matching the evidence, dates in a sane order).

Two conventions run through this module:

* **Nullable, not optional.** Every property is required and nullable rather than
  omissible. "The contract does not say" is a real and common answer, and a
  required null forces the model to state it instead of quietly dropping the key -
  which is indistinguishable from having missed it.
* **``evidence_chunk_ids`` on every item.** The model may only cite the chunk ids
  it was given, and the validator checks that. This is the mechanical half of
  "never invent contract content": an item whose citation is not in the supplied
  bundle is rejected regardless of how plausible its text reads.

Numeric and string constraints are deliberately absent - the provider's schema
compiler rejects them - so range checks live in
:mod:`app.ai.extraction.validation`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from app.core.enums import (
    DateType,
    EntityType,
    PartyRole,
    RiskSeverity,
    RiskType,
)


def _values(enum_cls: type[Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _obj(properties: dict[str, Any], *, required: list[str] | None = None) -> dict[str, Any]:
    """An object schema with every property required unless told otherwise."""
    return {
        "type": "object",
        "properties": properties,
        "required": required if required is not None else list(properties),
        "additionalProperties": False,
    }


def _array(items: dict[str, Any], description: str) -> dict[str, Any]:
    return {"type": "array", "items": items, "description": description}


def _str(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


def _req_str(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


def _num(description: str) -> dict[str, Any]:
    return {"type": ["number", "null"], "description": description}


def _int(description: str) -> dict[str, Any]:
    return {"type": ["integer", "null"], "description": description}


def _bool(description: str) -> dict[str, Any]:
    return {"type": ["boolean", "null"], "description": description}


def _enum(enum_cls: type[Enum], description: str, *, nullable: bool = True) -> dict[str, Any]:
    values = _values(enum_cls)
    if nullable:
        return {
            "type": ["string", "null"],
            "enum": [*values, None],
            "description": description,
        }
    return {"type": "string", "enum": values, "description": description}


# =============================================================================
# Shared fragments
# =============================================================================
#: The citation contract. Present on every extracted item.
_EVIDENCE = _array(
    {"type": "string"},
    "Ids of the evidence blocks this item was read from, exactly as labelled in "
    "square brackets. Only ids that appear in the supplied evidence are permitted.",
)

_CONFIDENCE = {
    "type": "number",
    "description": "Confidence in this extraction, 0.0 to 1.0. Use a value below 0.7 "
    "when the wording is ambiguous or the evidence is partial.",
}

_UNCERTAINTY = _str(
    "What is unclear or missing, if anything. Null when the evidence is complete. "
    "State uncertainty here rather than lowering the precision of the values."
)


def clause_schema(attribute_schema: dict[str, Any], *, clause_name: str) -> dict[str, Any]:
    """Schema for one clause category's extraction call.

    ``attribute_schema`` is the category's own contract from the Clause Master, so
    adding a clause type is a data change: no code here knows what a liability cap
    is.
    """
    return _obj(
        {
            "found": {
                "type": "boolean",
                "description": f"True only if the agreement actually contains a "
                f"{clause_name} clause in the supplied evidence.",
            },
            "clauses": _array(
                _obj(
                    {
                        "clause_number": _str(
                            "Clause number exactly as printed, e.g. '11.2'. Null if unnumbered."
                        ),
                        "title": _str("Heading as printed. Null if the clause has none."),
                        "text": _req_str(
                            "The clause text quoted verbatim from the evidence. Copy it "
                            "character for character; do not paraphrase, summarise, "
                            "correct or join separated passages."
                        ),
                        "summary": _str("One sentence in plain English on what it does."),
                        "attributes": attribute_schema,
                        "confidence": _CONFIDENCE,
                        "uncertainty": _UNCERTAINTY,
                        "evidence_chunk_ids": _EVIDENCE,
                    }
                ),
                f"Every distinct {clause_name} clause found. Empty when found is false.",
            ),
            "absence_reason": _str(
                "When found is false, why: 'not present in the agreement' or "
                "'evidence supplied does not cover it'. Null when found is true."
            ),
        }
    )


# =============================================================================
# Document-level categories
# =============================================================================
def metadata_schema() -> dict[str, Any]:
    """Document-level facts. One call, over the front matter and signature block."""
    return _obj(
        {
            "title": _str("Agreement title as printed on the document."),
            "agreement_type_stated": _str(
                "How the document describes itself, e.g. 'Master Services Agreement'."
            ),
            "summary": _req_str(
                "Three to five sentences on what this agreement does, who it binds "
                "and for how long. Drawn only from the supplied evidence."
            ),
            "key_topics": _array(
                {"type": "string"}, "Five to ten short topic labels for search and filtering."
            ),
            "contract_value": _num("Total stated value, as a number without symbols."),
            "currency": _str("ISO 4217 code for contract_value, e.g. USD, EUR, INR."),
            "effective_date": _str("Effective date as YYYY-MM-DD. Null if not stated."),
            "execution_date": _str("Signature date as YYYY-MM-DD. Null if not stated."),
            "expiration_date": _str("End date as YYYY-MM-DD. Null if not stated."),
            "term_months": _int("Initial term length in months, if expressed as a duration."),
            "confidence": _CONFIDENCE,
            "uncertainty": _UNCERTAINTY,
            "evidence_chunk_ids": _EVIDENCE,
        }
    )


def parties_schema() -> dict[str, Any]:
    """Contracting parties. Extracted before clauses.

    Party identity is a prerequisite, not a peer: attributes like "can we terminate"
    are resolved by matching a party name against the configured organisation
    aliases, so the parties must be known first.
    """
    return _obj(
        {
            "parties": _array(
                _obj(
                    {
                        "name": _req_str("Party name as used throughout the agreement."),
                        "legal_name": _str("Full registered legal name, if stated separately."),
                        "entity_type": _enum(EntityType, "What kind of entity this is."),
                        "role": _enum(PartyRole, "This party's role in the agreement."),
                        "aliases": _array(
                            {"type": "string"},
                            "Defined short forms, e.g. 'the Supplier', 'Customer'.",
                        ),
                        "jurisdiction": _str("State or country of incorporation."),
                        "registration_number": _str("Company or registration number."),
                        "address": _str("Notice address as printed."),
                        "contact_email": _str("Notice email address, if given."),
                        "contact_person": _str("Named contact or signatory."),
                        "is_primary": _bool("True for the two principal parties to the agreement."),
                        "confidence": _CONFIDENCE,
                        "evidence_chunk_ids": _EVIDENCE,
                    }
                ),
                "Every party named as a contracting entity.",
            ),
            "uncertainty": _UNCERTAINTY,
        }
    )


def obligations_schema() -> dict[str, Any]:
    """Duties, derived from the clauses already extracted."""
    return _obj(
        {
            "obligations": _array(
                _obj(
                    {
                        "action": _req_str("What must be done, as a single imperative statement."),
                        "responsible_party": _str("Which party owes this duty, by name."),
                        "due_date": _str("Absolute deadline as YYYY-MM-DD, if stated."),
                        "due_description": _str(
                            "Relative deadline as worded, e.g. 'within 30 days of invoice'."
                        ),
                        "trigger_event": _str("What starts the clock, if conditional."),
                        "dependency": _str("Another obligation this one depends on."),
                        "frequency": _str("For recurring duties: monthly, quarterly, annually."),
                        "is_recurring": _bool("True when the duty repeats."),
                        "penalty": _str("Stated consequence of failure, if any."),
                        "clause_type": _str("Clause category this duty arises from."),
                        "confidence": _CONFIDENCE,
                        "evidence_chunk_ids": _EVIDENCE,
                    }
                ),
                "Every obligation the agreement imposes on either party.",
            ),
            "uncertainty": _UNCERTAINTY,
        }
    )


def dates_schema() -> dict[str, Any]:
    """Key dates and deadlines, including relative ones."""
    return _obj(
        {
            "dates": _array(
                _obj(
                    {
                        "date_type": _enum(DateType, "What this date is.", nullable=False),
                        "date_value": _str("The date as YYYY-MM-DD, if absolute."),
                        "date_expression": _str(
                            "The wording when relative, e.g. '90 days before expiry'. "
                            "Always populate this when date_value is null."
                        ),
                        "description": _str("What happens on or by this date."),
                        "is_recurring": _bool("True when it recurs, e.g. annual review."),
                        "confidence": _CONFIDENCE,
                        "evidence_chunk_ids": _EVIDENCE,
                    }
                ),
                "Every date or deadline that carries a consequence.",
            ),
            "uncertainty": _UNCERTAINTY,
        }
    )


def risks_schema() -> dict[str, Any]:
    """Model-identified risks, on top of the deterministic rules.

    The rule engine already derives risks from clause attributes. This call exists
    for what rules cannot see - unusual drafting, an internal inconsistency, a
    one-sided remedy - and its findings are merged with, never substituted for, the
    deterministic ones.
    """
    return _obj(
        {
            "risks": _array(
                _obj(
                    {
                        "risk_type": _enum(
                            RiskType, "Closest matching risk category.", nullable=False
                        ),
                        "severity": _enum(RiskSeverity, "How serious this is.", nullable=False),
                        "description": _req_str(
                            "The risk in one or two sentences, referring to the actual "
                            "wording rather than generic contract advice."
                        ),
                        "recommendation": _str("What to negotiate or verify."),
                        "clause_type": _str("Clause category this risk arises from."),
                        "confidence": _CONFIDENCE,
                        "evidence_chunk_ids": _EVIDENCE,
                    }
                ),
                "Risks evidenced by the supplied text. Do not list generic risks that "
                "the evidence does not support.",
            ),
            "uncertainty": _UNCERTAINTY,
        }
    )


def relationships_schema() -> dict[str, Any]:
    """Cross-references and document relationships, for the knowledge graph."""
    return _obj(
        {
            "relationships": _array(
                _obj(
                    {
                        "relation": _req_str(
                            "One of: references, depends_on, defines, amends, replaces, "
                            "belongs_to, contains, governed_by, assigned_to, renewed_by."
                        ),
                        "source_ref": _req_str("Source, e.g. a clause number or party name."),
                        "source_type": _req_str("clause, party, contract, or term."),
                        "target_ref": _req_str("Target of the relationship."),
                        "target_type": _req_str("clause, party, contract, or term."),
                        "label": _str("How the agreement words the relationship."),
                        "confidence": _CONFIDENCE,
                        "evidence_chunk_ids": _EVIDENCE,
                    }
                ),
                "Explicit relationships stated in the text, including references to "
                "other agreements, schedules and exhibits.",
            ),
            "uncertainty": _UNCERTAINTY,
        }
    )


def financial_schema() -> dict[str, Any]:
    """Money terms. Separated from metadata because they carry their own evidence."""
    return _obj(
        {
            "total_value": _num("Total contract value as a number."),
            "currency": _str("ISO 4217 code."),
            "payment_days": _int("Days within which payment must be made, e.g. 30 for 'net 30'."),
            "payment_schedule": _str("How payment is structured: monthly, milestone, upfront."),
            "late_payment_interest_percent": _num("Interest rate on late payment, if stated."),
            "price_increase_cap_percent": _num("Cap on price increases, if stated."),
            "includes_taxes": _bool("True when stated amounts are inclusive of taxes."),
            "line_items": _array(
                _obj(
                    {
                        "description": _req_str("What is charged for."),
                        "amount": _num("Amount as a number."),
                        "currency": _str("ISO 4217 code."),
                        "frequency": _str("one_time, monthly, annual, per_unit."),
                    }
                ),
                "Individual charges, from a fee table or schedule where present.",
            ),
            "confidence": _CONFIDENCE,
            "uncertainty": _UNCERTAINTY,
            "evidence_chunk_ids": _EVIDENCE,
        }
    )


def summary_schema() -> dict[str, Any]:
    """Executive summary, generated after clause extraction.

    Written from the extracted clauses rather than from raw text so it cannot assert
    a term that extraction did not find - the summary is a view over verified facts,
    not a second, unchecked reading of the contract.
    """
    return _obj(
        {
            "executive_summary": _req_str(
                "Six to ten sentences for a reviewer who has not read the agreement: "
                "what it is, the commercial shape, and the terms that carry risk."
            ),
            "key_points": _array({"type": "string"}, "Five to eight single-sentence takeaways."),
            "parties_background": _str(
                "One or two plain sentences naming who the parties are and what the "
                "agreement is for, as you would explain it to someone who is not a "
                "lawyer. No clause numbers, no defined terms."
            ),
            "clause_digest": _array(
                _obj(
                    {
                        "clause_key": _req_str(
                            "The clause key exactly as given in EXTRACTED TERMS. Never "
                            "invent one and never emit a key that was not listed."
                        ),
                        "heading": _req_str(
                            "Short title for this clause as it should read in the "
                            "summary table, e.g. 'Ownership, Risk & Responsibility'."
                        ),
                        "lines": _array(
                            {"type": "string"},
                            "One to three short sentences in plain English saying what "
                            "this clause means in practice - who must do what, who "
                            "carries the risk, what the numbers are. Each sentence "
                            "stands alone; do not number them or repeat the heading.",
                        ),
                    }
                ),
                "One entry per clause listed in EXTRACTED TERMS, in the order given. "
                "Omit nothing that was listed and add nothing that was not.",
            ),
            "obligations_summary": _str("One paragraph on what each side must do."),
            "risk_summary": _str("One paragraph on where the exposure sits."),
            "uncertainty": _UNCERTAINTY,
        }
    )


#: Categories the engine runs, in order. Parties and metadata come first because
#: clause attributes are resolved against the party names they establish.
DOCUMENT_CATEGORIES: tuple[str, ...] = (
    "metadata",
    "parties",
    "clauses",
    "financial",
    "obligations",
    "dates",
    "risks",
    "relationships",
)

#: category -> prompt id, matching ``PROMPT_VERSIONS``.
CATEGORY_PROMPTS: dict[str, str] = {
    "metadata": "extraction.metadata",
    "parties": "extraction.parties",
    "clauses": "extraction.clauses",
    "financial": "extraction.financial",
    "obligations": "extraction.obligations",
    "rights": "extraction.rights",
    "risks": "extraction.risks",
    "dates": "extraction.dates",
    "relationships": "extraction.relationships",
}

#: category -> schema builder for the non-clause categories.
CATEGORY_SCHEMAS: dict[str, Any] = {
    "metadata": metadata_schema,
    "parties": parties_schema,
    "financial": financial_schema,
    "obligations": obligations_schema,
    "dates": dates_schema,
    "risks": risks_schema,
    "relationships": relationships_schema,
}


__all__ = [
    "CATEGORY_PROMPTS",
    "CATEGORY_SCHEMAS",
    "DOCUMENT_CATEGORIES",
    "clause_schema",
    "dates_schema",
    "financial_schema",
    "metadata_schema",
    "obligations_schema",
    "parties_schema",
    "relationships_schema",
    "risks_schema",
    "summary_schema",
]

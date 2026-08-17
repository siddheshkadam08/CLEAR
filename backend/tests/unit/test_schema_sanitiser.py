"""Schemas rewritten into what a strict structured-output engine will compile.

The rule that bit: every object must list *every* one of its properties in
``required``. Three seeded Clause Master rules have a nested array item with no
``required`` at all, and the provider answers with a 400 that costs the whole
category - ``notice`` and ``definitions`` both failed that way on a real run.
"""

from __future__ import annotations

from typing import Any

from app.ai.rag.providers import _sanitise_schema


def objects_with_incomplete_required(node: Any, path: tuple[str, ...] = ()) -> list[str]:
    """Every object whose ``required`` omits one of its properties."""
    found: list[str] = []
    if isinstance(node, dict):
        properties = node.get("properties")
        if (
            isinstance(properties, dict)
            and properties
            and set(properties) - set(node.get("required") or ())
        ):
            found.append(".".join(path) or "<root>")
        for key, value in node.items():
            found += objects_with_incomplete_required(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found += objects_with_incomplete_required(value, (*path, str(index)))
    return found


#: The shape that produced the 400, reduced to its essentials.
NOTICE_LIKE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notice_addresses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "party": {"type": ["string", "null"]},
                    "email": {"type": ["string", "null"]},
                },
            },
        }
    },
    "required": ["notice_addresses"],
}


def test_a_nested_item_gains_the_required_array_it_was_missing() -> None:
    cleaned = _sanitise_schema(NOTICE_LIKE)
    items = cleaned["properties"]["notice_addresses"]["items"]

    assert sorted(items["required"]) == ["email", "party"]
    assert objects_with_incomplete_required(cleaned) == []


def test_becoming_required_does_not_make_a_field_mandatory_to_answer() -> None:
    """Strict mode spells "may be absent" as a null in the type union.

    Without this the model cannot decline, and it invents an address rather than
    return nothing - which is worse than the 400 this replaces.
    """
    cleaned = _sanitise_schema(
        {
            "type": "object",
            "properties": {"coverage_type": {"type": "string"}},
        }
    )

    assert cleaned["required"] == ["coverage_type"]
    assert cleaned["properties"]["coverage_type"]["type"] == ["string", "null"]


def test_an_already_required_field_keeps_its_type() -> None:
    """Only fields made required by this pass are widened."""
    cleaned = _sanitise_schema(
        {
            "type": "object",
            "properties": {"clause_text": {"type": "string"}},
            "required": ["clause_text"],
        }
    )

    assert cleaned["properties"]["clause_text"]["type"] == "string"


def test_an_enum_gains_null_alongside_the_type() -> None:
    """A value outside the enumeration fails however the type is declared."""
    cleaned = _sanitise_schema(
        {
            "type": "object",
            "properties": {"cap_basis": {"type": "string", "enum": ["fees", "fixed"]}},
        }
    )

    assert cleaned["properties"]["cap_basis"]["type"] == ["string", "null"]
    assert cleaned["properties"]["cap_basis"]["enum"] == ["fees", "fixed", None]


def test_a_ref_or_anyof_branch_is_left_alone() -> None:
    """No ``type`` to widen, and rewriting one risks a schema that means something else."""
    cleaned = _sanitise_schema(
        {
            "type": "object",
            "properties": {"party": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        }
    )

    assert cleaned["properties"]["party"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert cleaned["required"] == ["party"]


def test_the_existing_guarantees_still_hold() -> None:
    """additionalProperties, and the unsupported keywords, are unchanged by this."""
    cleaned = _sanitise_schema(
        {
            "type": "object",
            "properties": {"amount": {"type": "number", "minimum": 0, "maximum": 10}},
        }
    )

    assert cleaned["additionalProperties"] is False
    assert "minimum" not in cleaned["properties"]["amount"]
    assert "maximum" not in cleaned["properties"]["amount"]


def test_an_open_object_is_carried_as_key_value_pairs() -> None:
    """An object with no properties cannot stay an object.

    This previously asserted that "an open object stays open" - it kept
    ``{"type": "object", "additionalProperties": false}``, which describes an
    object that may have no keys whatsoever. Strict mode does not treat that as a
    property at all, so the parent's ``required`` entry naming it became
    "Extra required key 'attributes' supplied" and the request 400ed.

    See :mod:`app.ai.rag.schema_compat` for the representation that replaces it.
    """
    cleaned = _sanitise_schema({"type": "object"})

    assert cleaned["type"] == "array"
    assert cleaned["items"]["required"] == ["key", "value"]
    assert cleaned["items"]["additionalProperties"] is False

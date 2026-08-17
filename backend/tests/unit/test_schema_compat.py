"""The schema actually sent to Azure, checked against the rules Azure applies.

Every assertion here runs over the **final** compiled schema - the object that
goes into ``response_format.json_schema.schema`` - rather than over the schema
the application wrote. That distinction is the whole point: the original schemas
are perfectly valid JSON Schema and were never the problem.

:func:`assert_strict_compatible` is a local re-implementation of the four rules
the provider's compiler enforces. It is deliberately strict and deliberately
independent of the code under test, so a change to the compiler that reintroduces
the 400 fails here rather than in production.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.ai.extraction.schemas import clause_schema, obligations_schema, parties_schema
from app.ai.rag.schema_compat import compile_strict, restore_payload
from app.db.clause_seeds import CLAUSE_SEEDS

# =============================================================================
# The provider's rules, restated
# =============================================================================
_UNSUPPORTED = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
}


def assert_strict_compatible(node: Any, path: str = "<root>") -> None:
    """Raise unless ``node`` satisfies every rule strict structured output has.

    The failure messages quote the path, because the provider's own 400 does the
    same (``In context=('properties', 'clauses', 'items')``) and matching them up
    is otherwise guesswork.
    """
    if isinstance(node, list):
        for index, item in enumerate(node):
            assert_strict_compatible(item, f"{path}[{index}]")
        return
    if not isinstance(node, dict):
        return

    for keyword in _UNSUPPORTED:
        assert keyword not in node, f"{path}: unsupported keyword {keyword!r} survived"

    assert node != {}, f"{path}: the empty schema is not a property the compiler can read"

    declared = node.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or ())
    properties = node.get("properties")

    if "object" in types or isinstance(properties, dict):
        assert isinstance(properties, dict) and properties, (
            f"{path}: an object with no properties is not expressible - "
            f"this is what produced 'Extra required key ... supplied'"
        )
        assert node.get("additionalProperties") is False, (
            f"{path}: every object must set additionalProperties=false"
        )
        required = node.get("required")
        assert isinstance(required, list), f"{path}: required must be supplied"
        assert set(required) == set(properties), (
            f"{path}: required must list every key in properties and nothing else. "
            f"missing={sorted(set(properties) - set(required))} "
            f"extra={sorted(set(required) - set(properties))}"
        )

    for key, value in node.items():
        if key == "properties":
            for name, sub in value.items():
                assert_strict_compatible(sub, f"{path}.{name}")
        elif key in {"items", "anyOf", "oneOf", "allOf", "$defs", "definitions"}:
            if isinstance(value, dict) and key in {"$defs", "definitions"}:
                for name, sub in value.items():
                    assert_strict_compatible(sub, f"{path}.{key}.{name}")
            else:
                assert_strict_compatible(value, f"{path}.{key}")


# =============================================================================
# The shapes named in the brief
# =============================================================================
def test_a_simple_object_is_closed_and_fully_required() -> None:
    compiled = compile_strict(
        {"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]}
    )

    assert compiled.schema["additionalProperties"] is False
    assert compiled.schema["required"] == ["title"]
    assert compiled.freeform_paths == ()
    assert_strict_compatible(compiled.schema)


def test_a_nested_object_is_repaired_at_every_depth() -> None:
    compiled = compile_strict(
        {
            "type": "object",
            "properties": {
                "cap": {
                    "type": "object",
                    "properties": {"amount": {"type": "number"}, "currency": {"type": "string"}},
                }
            },
        }
    )

    cap = compiled.schema["properties"]["cap"]
    assert sorted(cap["required"]) == ["amount", "currency"]
    assert cap["additionalProperties"] is False
    assert_strict_compatible(compiled.schema)


def test_an_array_of_objects_is_repaired_inside_items() -> None:
    compiled = compile_strict(
        {
            "type": "object",
            "properties": {
                "parties": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"name": {"type": "string"}}},
                }
            },
        }
    )

    assert compiled.schema["properties"]["parties"]["items"]["required"] == ["name"]
    assert_strict_compatible(compiled.schema)


def test_a_nullable_property_is_left_exactly_as_written() -> None:
    compiled = compile_strict(
        {
            "type": "object",
            "properties": {"note": {"type": ["string", "null"]}},
            "required": ["note"],
        }
    )

    assert compiled.schema["properties"]["note"]["type"] == ["string", "null"]


def test_an_optional_property_becomes_required_and_nullable() -> None:
    """Strict mode has no "optional"; required-and-nullable means the same thing."""
    compiled = compile_strict(
        {
            "type": "object",
            "properties": {"kept": {"type": "string"}, "optional": {"type": "integer"}},
            "required": ["kept"],
        }
    )

    assert sorted(compiled.schema["required"]) == ["kept", "optional"]
    assert compiled.schema["properties"]["kept"]["type"] == "string"
    assert compiled.schema["properties"]["optional"]["type"] == ["integer", "null"]


@pytest.mark.parametrize(
    "freeform",
    [
        pytest.param({}, id="empty-schema"),
        pytest.param({"type": "object"}, id="object-without-properties"),
        pytest.param({"type": "object", "properties": {}}, id="object-with-empty-properties"),
        pytest.param(
            {"type": "object", "additionalProperties": True}, id="explicitly-open-object"
        ),
    ],
)
def test_every_spelling_of_a_free_form_dictionary_is_carried_as_pairs(
    freeform: dict[str, Any],
) -> None:
    """All four mean "any keys at all", which strict mode cannot express."""
    compiled = compile_strict(
        {"type": "object", "properties": {"attributes": freeform}, "required": ["attributes"]}
    )

    attributes = compiled.schema["properties"]["attributes"]
    assert attributes["type"] == "array"
    assert sorted(attributes["items"]["properties"]) == ["key", "value"]
    assert compiled.freeform_paths == ((("prop", "attributes"),),)
    assert_strict_compatible(compiled.schema)


def test_a_typed_property_without_properties_is_not_mistaken_for_free_form() -> None:
    """Only object-ish nodes are rewritten; a plain string must survive."""
    compiled = compile_strict(
        {"type": "object", "properties": {"name": {"type": "string", "description": "x"}}}
    )

    assert compiled.schema["properties"]["name"]["type"] == ["string", "null"]
    assert compiled.freeform_paths == ()


# =============================================================================
# The failure that was reported
# =============================================================================
def test_the_reported_400_cannot_recur_on_clauses_items_attributes() -> None:
    """`clauses -> items -> attributes`, the exact context Azure named.

    A Clause Master row with no ``output_schema`` reaches ``clause_schema`` as
    ``{}`` (``ExtractionEngine`` does ``dict(rule.output_schema or {})``). Before
    the fix that stayed ``{}`` in ``properties`` while ``required`` still named
    it, which is precisely "Extra required key 'attributes' supplied".
    """
    compiled = compile_strict(clause_schema({}, clause_name="Confidentiality"))
    item = compiled.schema["properties"]["clauses"]["items"]

    assert "attributes" in item["properties"]
    assert "attributes" in item["required"]
    assert set(item["required"]) == set(item["properties"])
    assert item["properties"]["attributes"] != {}
    assert item["properties"]["attributes"]["type"] == "array"
    assert compiled.freeform_paths == (
        (("prop", "clauses"), ("items", None), ("prop", "attributes")),
    )
    assert_strict_compatible(compiled.schema)


def test_a_clause_schema_with_a_real_attribute_contract_keeps_its_object() -> None:
    """A category that declares attributes is not rewritten - nothing to fix."""
    contract = {
        "type": "object",
        "properties": {
            "cap_basis": {"type": ["string", "null"], "enum": ["fees", "fixed", None]},
            "cap_amount": {"type": ["number", "null"]},
        },
    }
    compiled = compile_strict(clause_schema(contract, clause_name="Limitation of Liability"))
    attributes = compiled.schema["properties"]["clauses"]["items"]["properties"]["attributes"]

    assert attributes["type"] == "object"
    assert sorted(attributes["required"]) == ["cap_amount", "cap_basis"]
    assert compiled.freeform_paths == ()
    assert_strict_compatible(compiled.schema)


@pytest.mark.parametrize("seed", CLAUSE_SEEDS, ids=lambda s: s.key)
def test_every_seeded_clause_master_schema_compiles(seed: Any) -> None:
    """The nested Clause Master contracts, as actually seeded."""
    compiled = compile_strict(clause_schema(seed.output_schema, clause_name=seed.name))
    assert_strict_compatible(compiled.schema)


@pytest.mark.parametrize(
    "builder", [parties_schema, obligations_schema], ids=["parties", "obligations"]
)
def test_the_document_level_schemas_compile(builder: Any) -> None:
    assert_strict_compatible(compile_strict(builder()).schema)


# =============================================================================
# Restoration
# =============================================================================
def test_the_response_is_restored_to_the_dictionary_the_application_expects() -> None:
    compiled = compile_strict(clause_schema({}, clause_name="Confidentiality"))
    wire = {
        "found": True,
        "clauses": [
            {
                "text": "...",
                "attributes": [
                    {"key": "term_years", "value": "3"},
                    {"key": "perpetual", "value": "false"},
                    {"key": "cap_basis", "value": "1x_fees_paid"},
                    {"key": "carve_outs", "value": '["fraud", "IP"]'},
                    {"key": "missing", "value": None},
                ],
            }
        ],
    }

    restored = restore_payload(wire, compiled.freeform_paths)
    attributes = restored["clauses"][0]["attributes"]

    assert isinstance(attributes, dict)
    # Types are recovered where the text says so unambiguously...
    assert attributes["term_years"] == 3
    assert attributes["perpetual"] is False
    assert attributes["carve_outs"] == ["fraud", "IP"]
    # ...and left alone where it does not. `1x_fees_paid` is not a number.
    assert attributes["cap_basis"] == "1x_fees_paid"
    assert attributes["missing"] is None


def test_restoration_tolerates_a_model_that_returned_the_object_anyway() -> None:
    compiled = compile_strict({"type": "object", "properties": {"attributes": {}}})
    restored = restore_payload({"attributes": {"already": "a dict"}}, compiled.freeform_paths)

    assert restored["attributes"] == {"already": "a dict"}


def test_restoration_never_raises_on_a_shape_it_does_not_recognise() -> None:
    """Losing a category to a TypeError here would be worse than the oddity."""
    compiled = compile_strict(clause_schema({}, clause_name="X"))

    for payload in ({}, {"clauses": None}, {"clauses": ["not-an-object"]}, {"clauses": [{}]}):
        restore_payload(payload, compiled.freeform_paths)


def test_restoration_is_a_no_op_when_nothing_was_rewritten() -> None:
    compiled = compile_strict({"type": "object", "properties": {"a": {"type": "string"}}})
    payload = {"a": "x"}

    assert restore_payload(payload, compiled.freeform_paths) is payload


# =============================================================================
# Hygiene
# =============================================================================
def test_the_callers_schema_is_never_mutated() -> None:
    """Schemas come from the Clause Master and are reused across every chunk."""
    original = clause_schema({}, clause_name="Confidentiality")
    before = json.dumps(original, sort_keys=True)

    compile_strict(original)

    assert json.dumps(original, sort_keys=True) == before


def test_a_property_named_like_a_keyword_is_not_stripped() -> None:
    """`properties` holds names, not schema vocabulary."""
    compiled = compile_strict(
        {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "maximum": {"type": "number"},
            },
        }
    )

    assert sorted(compiled.schema["properties"]) == ["maximum", "pattern"]
    assert_strict_compatible(compiled.schema)

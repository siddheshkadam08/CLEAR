"""Make a JSON Schema acceptable to strict structured outputs, and undo it after.

Azure OpenAI and OpenAI enforce ``response_format={"type": "json_schema",
"strict": true}`` by *compiling* the schema. The compiler accepts a subset of
JSON Schema and rejects the request outright - HTTP 400, the whole extraction
category lost - when anything falls outside it. This module owns that subset, in
one place, so no prompt or call site has to know about it.

Four rules, and the fourth is the one that bit us:

1. Every object carries ``additionalProperties: false``.
2. Every object's ``required`` lists **every** key in ``properties`` - no more and
   no less. Optionality is expressed by widening the type to accept ``null``,
   which is how strict mode expects "may be absent" to be written.
3. Numeric and string constraints (``minimum``, ``pattern``, ...) are not
   supported and are dropped. They are re-checked by
   :mod:`app.ai.extraction.validation`, which is where a range violation belongs
   anyway - it is a data-quality finding, not a parse failure.
4. **A free-form object is not expressible.** ``{}``, ``{"type": "object"}`` with
   no properties, and ``additionalProperties: true`` all mean "any keys at all",
   and strict mode has no way to say that. The compiler does not report it as an
   unsupported feature; it simply does not treat the node as a property, and then
   blames the ``required`` entry that names it:

       Invalid schema for response_format 'extraction':
       In context=('properties', 'clauses', 'items'),
       required is required to be supplied and to be an array including every key
       in properties. Extra required key 'attributes' supplied.

   Which reads as a bug in ``required`` and is nothing of the sort - ``required``
   was right, and ``properties['attributes']`` was the empty schema ``{}``.

   That empty schema is reachable from ordinary data:
   ``ExtractionEngine`` builds a clause definition with
   ``output_schema=dict(rule.output_schema or {})``, so a Clause Master row with
   no attribute contract yields ``{}``, which becomes
   ``clauses.items.attributes``.

**How a free-form object is carried instead.** It is rewritten as an array of
``{key, value}`` pairs, which strict mode expresses perfectly, and converted back
into a dictionary by :func:`restore_payload` before the caller ever sees it. So
the application keeps receiving ``attributes`` as a ``dict`` - the clause rules in
``validate_clause_attributes`` and the ``attributes`` JSONB column are untouched -
and only the wire format differs.

The cost is that a value's JSON type is not declared, so it arrives as a string.
:func:`_decode` recovers numbers, booleans, ``null`` and nested JSON where the
text unambiguously says so, and leaves anything else as the string it is.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

#: Keywords the strict compiler rejects. Dropped here, enforced in validation.
UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {
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
        "default",
        "examples",
        "format",
    }
)

#: Keywords whose value is a *map of subschemas*, so their keys are names rather
#: than schema vocabulary and must not be filtered as keywords.
_SUBSCHEMA_MAPS: frozenset[str] = frozenset({"properties", "$defs", "definitions"})

#: Keywords whose value is a list of subschemas.
_SUBSCHEMA_LISTS: frozenset[str] = frozenset({"anyOf", "oneOf", "allOf"})

#: One step of a path into a *response payload*: a property name, or "every
#: element of this array".
PathStep = tuple[Literal["prop"], str] | tuple[Literal["items"], None]
Path = tuple[PathStep, ...]

_ITEMS: PathStep = ("items", None)


def _prop(name: str) -> PathStep:
    return ("prop", name)


@dataclass(slots=True)
class CompiledSchema:
    """A provider-ready schema plus what has to be undone in the response."""

    schema: dict[str, Any]
    #: Where a free-form object was rewritten as a key/value array. Empty for the
    #: overwhelming majority of schemas, which need no restoration at all.
    freeform_paths: tuple[Path, ...] = field(default_factory=tuple)

    @property
    def rewrote_freeform(self) -> bool:
        return bool(self.freeform_paths)


# =============================================================================
# Compile
# =============================================================================
def compile_strict(schema: dict[str, Any]) -> CompiledSchema:
    """Return ``schema`` in the form strict structured outputs accepts.

    The input is never mutated: every container is rebuilt. Callers pass schemas
    that come from the Clause Master and are reused across thousands of chunks,
    so mutating one would corrupt every later call in the process.
    """
    found: list[Path] = []
    compiled = _compile(schema, path=(), found=found)
    if not isinstance(compiled, dict):  # pragma: no cover - a schema is an object
        compiled = {}
    return CompiledSchema(schema=compiled, freeform_paths=tuple(found))


def _compile(node: Any, *, path: Path, found: list[Path]) -> Any:
    if isinstance(node, list):
        return [_compile(item, path=path, found=found) for item in node]
    if not isinstance(node, dict):
        return node

    # A free-form object cannot be expressed; carry it as key/value pairs.
    if _is_freeform_object(node):
        found.append(path)
        return _freeform_schema(node)

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in UNSUPPORTED_KEYWORDS:
            continue
        if key in _SUBSCHEMA_MAPS:
            # Keys here are property names, not schema keywords, so a property
            # legitimately called "pattern" or "format" must survive.
            result[key] = {
                name: _compile(
                    sub,
                    path=(*path, _prop(name)) if key == "properties" else path,
                    found=found,
                )
                for name, sub in value.items()
            }
        elif key in _SUBSCHEMA_LISTS:
            result[key] = [_compile(sub, path=path, found=found) for sub in value]
        elif key == "items":
            result[key] = _compile(value, path=(*path, _ITEMS), found=found)
        else:
            result[key] = _compile(value, path=path, found=found)

    if _is_object(result):
        properties = result.get("properties")
        if isinstance(properties, dict) and properties:
            required = set(result.get("required") or ())
            for name, sub in properties.items():
                if name not in required:
                    # Optional in the source: strict mode has no "optional", so it
                    # becomes required-and-nullable, which means the same thing.
                    properties[name] = _accepts_null(sub)
            result["required"] = list(properties)
        result["additionalProperties"] = False

    return result


def _is_object(node: dict[str, Any]) -> bool:
    """Does this node describe a JSON object?

    ``type`` may be a union (``["object", "null"]``), and a node carrying
    ``properties`` describes an object whether or not it says so.
    """
    declared = node.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or ())
    return "object" in types or isinstance(node.get("properties"), dict)


def _is_freeform_object(node: dict[str, Any]) -> bool:
    """Is this "an object with any keys at all"?

    Three spellings, all unrepresentable in strict mode:

    * ``{}`` - the empty schema, which accepts any value. This is what a Clause
      Master row with no ``output_schema`` produces.
    * ``{"type": "object"}`` with no ``properties``, or with an empty one.
    * an object that explicitly permits extra keys.

    A node with a ``$ref`` is left alone: it is not ours to rewrite, and guessing
    risks producing a schema that compiles and means something else.
    """
    if "$ref" in node:
        return False
    if not node:
        return True

    declared = node.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or ())
    properties = node.get("properties")

    if "object" not in types and not isinstance(properties, dict):
        # No type and no properties, but some other keyword (an enum, a
        # description). Not an object - leave it be.
        return False
    if node.get("additionalProperties") is True:
        return True
    return not (isinstance(properties, dict) and properties)


def _freeform_schema(node: dict[str, Any]) -> dict[str, Any]:
    """The key/value-array stand-in for a free-form object."""
    description = str(node.get("description") or "").strip()
    guidance = (
        "Return the attributes as a list of {key, value} pairs - one entry per "
        "attribute you can support from the evidence, and an empty list when the "
        "evidence supports none. Write each value as plain text; use JSON for a "
        "number, true/false, or a list."
    )
    return {
        "type": "array",
        "description": f"{description} {guidance}".strip(),
        "items": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "The attribute name, in snake_case.",
                },
                "value": {
                    "type": ["string", "null"],
                    "description": "The attribute value. Null when not stated.",
                },
            },
            "required": ["key", "value"],
            "additionalProperties": False,
        },
    }


def _accepts_null(subschema: Any) -> Any:
    """Widen a subschema so ``null`` is a valid value.

    Left alone when the type is already nullable, or when there is no ``type`` to
    widen - a ``$ref`` or an ``anyOf`` branch is not ours to rewrite. An ``enum``
    gains ``null`` alongside the type, since a value outside the enumeration fails
    validation however the type is declared.
    """
    if not isinstance(subschema, dict):
        return subschema
    declared = subschema.get("type")
    if declared is None:
        return subschema

    types = list(declared) if isinstance(declared, list) else [declared]
    if "null" in types:
        return subschema

    widened = dict(subschema)
    widened["type"] = [*types, "null"]
    enum = widened.get("enum")
    if isinstance(enum, list) and None not in enum:
        widened["enum"] = [*enum, None]
    return widened


# =============================================================================
# Restore
# =============================================================================
#: A value worth trying to decode as JSON. Anything else is left as text, so
#: "1x_fees_paid" and "Acme, Inc." are never mangled into something else.
_JSON_ISH = re.compile(r"^\s*(-?\d+(\.\d+)?([eE][-+]?\d+)?|true|false|null|\[.*\]|\{.*\})\s*$", re.S)


def restore_payload(data: Any, freeform_paths: tuple[Path, ...]) -> Any:
    """Turn every rewritten key/value array back into a dictionary.

    A no-op when nothing was rewritten, which is the common case. Never raises on
    a shape it does not recognise: a model that returned something unexpected is
    the validator's problem, not the transport's, and losing the whole category to
    a ``TypeError`` here would be far worse than passing the oddity along.
    """
    if not freeform_paths:
        return data
    for path in freeform_paths:
        _restore_at(data, path)
    return data


def _restore_at(node: Any, path: Path) -> None:
    if not path:
        return
    step, rest = path[0], path[1:]

    if step[0] == "items":
        if isinstance(node, list):
            for item in node:
                _restore_at(item, rest)
        return

    name = step[1]
    if not isinstance(node, dict) or name not in node:
        return

    if rest:
        _restore_at(node[name], rest)
        return

    node[name] = _pairs_to_dict(node[name])


def _pairs_to_dict(value: Any) -> Any:
    """``[{"key": k, "value": v}, ...]`` -> ``{k: v}``.

    A duplicated key keeps the last entry, matching how a JSON object would have
    behaved had the model been able to emit one.
    """
    if isinstance(value, dict):
        # Some models emit the object anyway. Take it as-is - it is what the
        # application wanted in the first place.
        return value
    if not isinstance(value, list):
        return {}

    restored: dict[str, Any] = {}
    for entry in value:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            continue
        restored[key] = _decode(entry.get("value"))
    return restored


def _decode(value: Any) -> Any:
    """Recover a JSON scalar from its text form, conservatively."""
    if not isinstance(value, str):
        return value
    if not _JSON_ISH.match(value):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


__all__ = [
    "UNSUPPORTED_KEYWORDS",
    "CompiledSchema",
    "Path",
    "compile_strict",
    "restore_payload",
]

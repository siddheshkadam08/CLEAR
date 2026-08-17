"""What OpenAIProvider actually puts on the wire for a structured call.

The schema tests prove the compiler is correct in isolation. This proves the
provider *uses* it: that the request carries
``response_format.type == "json_schema"`` with ``strict: true``, that the schema
inside it is the compiled one rather than the caller's, and that the response is
converted back before the caller sees it.

The client is stubbed at ``chat.completions.create`` - the single call
``_invoke`` makes - so the assertions are about the request the SDK would have
sent, not about a re-implementation of it.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai.extraction.schemas import clause_schema
from app.ai.rag.openai_provider import OpenAIProvider
from tests.unit.test_schema_compat import assert_strict_compatible


class _CapturingCompletions:
    """Records the request and answers with a fixed, schema-shaped payload."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        return SimpleNamespace(
            model=request["model"],
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, prompt_tokens_details=None
            ),
        )


def _provider(content: str) -> tuple[OpenAIProvider, _CapturingCompletions]:
    provider = OpenAIProvider(azure=True)
    completions = _CapturingCompletions(content)
    # Bypass _get_client: no endpoint, no credential, no network.
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return provider, completions


#: A clause category with no attribute contract - the case that 400ed.
_FREEFORM_CLAUSE = clause_schema({}, clause_name="Confidentiality")

_WIRE_RESPONSE = json.dumps(
    {
        "found": True,
        "clauses": [
            {
                "clause_number": "11.2",
                "title": "Confidentiality",
                "text": "Each party shall keep confidential...",
                "summary": "Mutual confidentiality obligation.",
                "attributes": [
                    {"key": "term_years", "value": "3"},
                    {"key": "mutual", "value": "true"},
                    {"key": "cap_basis", "value": "1x_fees_paid"},
                ],
                "confidence": 0.91,
                "uncertainty": None,
                "evidence_chunk_ids": ["c1"],
            }
        ],
        "absence_reason": None,
    }
)


@pytest.mark.asyncio
async def test_the_request_declares_strict_json_schema() -> None:
    provider, completions = _provider(_WIRE_RESPONSE)

    await provider.generate_structured(
        system="s", prompt="p", schema=_FREEFORM_CLAUSE, purpose="extraction"
    )

    response_format = completions.requests[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert response_format["json_schema"]["name"] == "extraction"


@pytest.mark.asyncio
async def test_the_schema_on_the_wire_is_provider_compatible() -> None:
    """The whole schema, checked against the provider's own rules."""
    provider, completions = _provider(_WIRE_RESPONSE)

    await provider.generate_structured(
        system="s", prompt="p", schema=_FREEFORM_CLAUSE, purpose="extraction"
    )

    assert_strict_compatible(completions.requests[0]["response_format"]["json_schema"]["schema"])


@pytest.mark.asyncio
async def test_clauses_items_attributes_is_sent_in_the_repaired_form() -> None:
    """The exact context Azure named in the 400."""
    provider, completions = _provider(_WIRE_RESPONSE)

    await provider.generate_structured(
        system="s", prompt="p", schema=_FREEFORM_CLAUSE, purpose="extraction"
    )

    sent = completions.requests[0]["response_format"]["json_schema"]["schema"]
    item = sent["properties"]["clauses"]["items"]

    # The two halves of "Extra required key 'attributes' supplied".
    assert "attributes" in item["properties"], "the key Azure could not find"
    assert set(item["required"]) == set(item["properties"]), "required must match properties"
    # And the reason it could not find it: an empty schema is not a property.
    assert item["properties"]["attributes"] != {}
    assert item["properties"]["attributes"]["type"] == "array"


@pytest.mark.asyncio
async def test_the_caller_still_receives_attributes_as_a_dictionary() -> None:
    """The application contract is unchanged: `attributes` is a dict."""
    provider, _ = _provider(_WIRE_RESPONSE)

    result = await provider.generate_structured(
        system="s", prompt="p", schema=_FREEFORM_CLAUSE, purpose="extraction"
    )

    attributes = result.data["clauses"][0]["attributes"]
    assert isinstance(attributes, dict)
    assert attributes == {"term_years": 3, "mutual": True, "cap_basis": "1x_fees_paid"}


@pytest.mark.asyncio
async def test_a_declared_attribute_contract_is_sent_unrewritten() -> None:
    """Only free-form objects are rewritten; a real contract goes as an object."""
    schema = clause_schema(
        {"type": "object", "properties": {"cap_amount": {"type": ["number", "null"]}}},
        clause_name="Limitation of Liability",
    )
    payload = json.dumps(
        {"found": True, "clauses": [{"text": "t", "attributes": {"cap_amount": 500000.0}}]}
    )
    provider, completions = _provider(payload)

    result = await provider.generate_structured(
        system="s", prompt="p", schema=schema, purpose="extraction"
    )

    sent = completions.requests[0]["response_format"]["json_schema"]["schema"]
    assert sent["properties"]["clauses"]["items"]["properties"]["attributes"]["type"] == "object"
    assert result.data["clauses"][0]["attributes"] == {"cap_amount": 500000.0}

#!/usr/bin/env python3
"""End-to-end proof that a strict structured extraction call actually works.

Runs the real provider against the configured Azure OpenAI deployment with the
schema that used to 400 - a clause category whose Clause Master row carries no
attribute contract, so ``clauses.items.attributes`` is the free-form object the
strict compiler cannot express.

Prints ``STRUCTURED TEST SUCCESS`` and the parsed result on success, and exits
non-zero on any failure, so it is usable as a deployment gate:

    python scripts/smoke_structured_output.py

Deliberately prints no credential, no endpoint and no contract text beyond the
few lines of synthetic agreement defined here - it is safe to run in a shared
terminal and to paste the output into a ticket.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

#: A short, invented agreement. Not customer data.
_EVIDENCE = """
[c1] 11.2 Confidentiality. Each party shall keep confidential all Confidential
Information disclosed by the other party and shall not disclose it to any third
party for a period of three (3) years from the date of disclosure. The receiving
party may disclose Confidential Information to its professional advisers.
"""


async def main() -> int:
    from app.ai.extraction.schemas import clause_schema
    from app.ai.rag.providers import get_inference_provider
    from app.ai.rag.schema_compat import compile_strict
    from app.core.config import get_settings

    settings = get_settings()
    provider = get_inference_provider()

    print(f"provider   : {provider.name}")
    print(f"deployment : {settings.llm.azure_openai_deployment or settings.llm.model}")
    print(f"structured : {settings.llm.llm_structured_output}")

    if provider.name == "mock":
        print("FAIL       : the mock provider proves nothing. Configure LLM_PROVIDER.")
        return 2

    # The failing case: no attribute contract, so `attributes` is `{}`.
    schema = clause_schema({}, clause_name="Confidentiality")
    compiled = compile_strict(schema)
    attributes = compiled.schema["properties"]["clauses"]["items"]["properties"]["attributes"]
    print(f"attributes : sent as {attributes['type']} (was the empty schema)")
    print()

    result = await provider.generate_structured(
        system=(
            "You extract clauses from contracts. Quote text verbatim from the "
            "evidence and cite only the bracketed ids you were given."
        ),
        prompt=(
            "Extract the Confidentiality clause from this evidence.\n"
            f"{_EVIDENCE}\n"
            "Record the confidentiality period as an attribute."
        ),
        schema=schema,
        purpose="extraction",
    )

    data = result.data
    clauses = data.get("clauses") or []
    if not isinstance(data.get("found"), bool):
        print(f"FAIL       : 'found' missing or not a boolean: {data!r}")
        return 1
    if not clauses:
        print(f"FAIL       : no clause returned: {json.dumps(data)[:400]}")
        return 1

    first = clauses[0]
    clause_attributes = first.get("attributes")
    if not isinstance(clause_attributes, dict):
        print(
            "FAIL       : attributes came back as "
            f"{type(clause_attributes).__name__}, not the dict the application needs."
        )
        return 1

    print(f"model      : {result.inference.model}")
    print(f"tokens     : {result.usage.total}   cost: ${result.cost_usd:.6f}")
    print()
    print("clause     :", json.dumps(first.get("title") or first.get("clause_number")))
    print("text       :", json.dumps((first.get("text") or "")[:120]))
    print("attributes :", json.dumps(clause_attributes))
    print("citations  :", json.dumps(first.get("evidence_chunk_ids")))
    print()
    print("STRUCTURED TEST SUCCESS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Deterministic mock inference provider.

Generates schema-conformant output by walking the JSON Schema the caller supplied,
which makes the entire pipeline - classification, extraction, risk scoring,
embedding, retrieval, answering - runnable in CI and on a laptop with no API key
and no network.

Two properties make it useful rather than merely inert:

* **Schema-driven.** Output is synthesised from the requested schema, so the
  extraction validator, the risk engine and the UI all receive realistically
  shaped data. A stub returning ``{}`` would let schema bugs through undetected.
* **Deterministic.** Values derive from a hash of the prompt, so a test asserting
  on clause counts or risk bands stays stable across runs.
* **Grounded where grounding is required.** Citation fields are answered with
  evidence block ids taken from the prompt, and fields the schema requires to be
  verbatim quotes are answered with real sentences from that evidence. A stub that
  invented these would be rejected wholesale by extraction validation - correctly,
  since inventing them is exactly what the validator exists to catch - and the
  pipeline could never be exercised end to end. Fabrication is still tested, by
  handing the validator text that is genuinely absent from the evidence.

It is not a quality substitute for a real model: values are plausible-shaped, not
correct. Production refuses to start with ``LLM_PROVIDER=mock``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from app.ai.rag.providers import (
    IInferenceProvider,
    InferenceResult,
    Purpose,
    StructuredResult,
    TokenUsage,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Values used when a schema property carries no enum. Keyed by property-name
#: fragment so generated data reads like contract data rather than "string".
_HINTS: tuple[tuple[tuple[str, ...], Any], ...] = (
    (("notice_days", "cure_period_days", "payment_days", "renewal_notice_days"), 30),
    (("duration_months", "term_months", "renewal_term_months"), 24),
    (("duration_years", "survival_years"), 5.0),
    (("cap_multiple",), 2.0),
    (("amount", "cap_amount", "minimum_amount", "contract_value"), 50000.0),
    (("percent", "rate_percent", "uptime_percent"), 1.5),
    (("currency",), "USD"),
    (("governing_law",), "laws of the State of Delaware"),
    (("country",), "United States"),
    (("jurisdiction", "venue", "seat"), "Delaware"),
    (("language",), "English"),
    (("date", "deadline"), "2027-12-31"),
    (("party", "licensor", "licensee", "vendor", "customer"), "Acme Corporation"),
    (("name", "title", "owner"), "Master Services Agreement"),
    (("email",), "legal@example.com"),
    (("address",), "1 Example Plaza, Wilmington, DE"),
    (
        ("description", "summary", "text", "reason", "recommendation"),
        "Extracted from the agreement text.",
    ),
)

#: Fields whose value must be an evidence block id the caller supplied. Answering
#: these from the prompt is what makes the stub's output survive citation checks.
_EVIDENCE_ID_KEYS: frozenset[str] = frozenset({"evidence_chunk_ids", "chunk_id", "chunk_ids"})

#: Fields the extraction schemas require to be verbatim quotes from the evidence.
_VERBATIM_KEYS: frozenset[str] = frozenset({"text", "carve_out_text", "quote"})

#: ``[id] header\nbody`` blocks as ``EvidenceBundle.render`` writes them, separated
#: by a ``---`` rule.
_EVIDENCE_BLOCK = re.compile(
    r"^\[(?P<id>[^\]\n]+)\](?P<body>.*?)(?=\n\n---\n\n\[|\Z)",
    re.MULTILINE | re.DOTALL,
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


class MockInferenceProvider(IInferenceProvider):
    """Deterministic, schema-conformant stub."""

    name = "mock"

    def __init__(self) -> None:
        #: Recorded calls, so a test can assert on prompts and routing.
        self.calls: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _seed(prompt: str) -> int:
        return int(hashlib.sha256(prompt.encode()).hexdigest()[:8], 16)

    @staticmethod
    def _usage(system: str, prompt: str, output: str) -> TokenUsage:
        """Approximate usage. ~4 characters per token is close enough for a stub."""
        return TokenUsage(
            input_tokens=(len(system) + len(prompt)) // 4,
            output_tokens=max(1, len(output) // 4),
        )

    # ---------------------------------------------------------------- generation
    async def generate(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose = "rag",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
        surface_thinking: bool = False,
    ) -> InferenceResult:
        started = time.perf_counter()
        self.calls.append({"kind": "generate", "purpose": purpose, "prompt": prompt})

        text = self._narrative(prompt, purpose)
        return InferenceResult(
            text=text,
            model=f"mock-{self.route_model(purpose)}",
            usage=self._usage(system, prompt, text),
            latency_ms=int((time.perf_counter() - started) * 1000),
            stop_reason="end_turn",
            thinking="Deterministic mock reasoning." if surface_thinking else None,
            provider=self.name,
        )

    async def generate_structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        purpose: Purpose = "extraction",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
    ) -> StructuredResult:
        started = time.perf_counter()
        self.calls.append(
            {"kind": "structured", "purpose": purpose, "prompt": prompt, "schema": schema}
        )
        seed = self._seed(prompt)
        generated = self._from_schema(schema, seed=seed, prompt=prompt)
        data = generated if isinstance(generated, dict) else {}
        rendered = json.dumps(data)
        return StructuredResult(
            data=data,
            inference=InferenceResult(
                text=rendered,
                model=f"mock-{self.route_model(purpose)}",
                usage=self._usage(system, prompt, rendered),
                latency_ms=int((time.perf_counter() - started) * 1000),
                stop_reason="end_turn",
                provider=self.name,
            ),
        )

    async def stream(
        self,
        *,
        system: str,
        prompt: str,
        purpose: Purpose = "rag",
        max_tokens: int | None = None,
        cache_prefix: bool = True,
    ) -> AsyncIterator[str]:
        self.calls.append({"kind": "stream", "purpose": purpose, "prompt": prompt})
        for word in self._narrative(prompt, purpose).split(" "):
            yield word + " "

    async def health(self) -> bool:
        return True

    # ------------------------------------------------------------------ narrative
    @staticmethod
    def _narrative(prompt: str, purpose: Purpose) -> str:
        """A grounded-looking answer that still carries a citation marker.

        Includes ``[1]`` so the response validator's citation-coverage check
        exercises its real path rather than trivially failing.
        """
        if purpose == "summary":
            return (
                "This agreement establishes a 24-month engagement commencing "
                "1 January 2026, with payment due within 30 days of invoice [1]. "
                "Liability is capped at two times fees paid, subject to carve-outs "
                "for confidentiality and IP infringement [1]. Either party may "
                "terminate for convenience on 60 days' notice [1]."
            )
        if purpose in {"comparison", "report"}:
            return (
                "Across the contracts reviewed, liability caps cluster at one to two "
                "times fees paid, with two agreements uncapped for IP infringement [1]. "
                "Payment terms range from 30 to 60 days [1]."
            )
        return (
            "Based on the retrieved clauses, the agreement caps aggregate liability at "
            "two times the fees paid in the preceding twelve months, with carve-outs "
            "for breach of confidentiality, IP infringement and gross negligence [1]."
        )

    # --------------------------------------------------------------- schema walk
    def _from_schema(
        self,
        schema: dict[str, Any],
        *,
        seed: int,
        prompt: str,
        key: str = "",
        depth: int = 0,
    ) -> Any:
        """Synthesise a value satisfying ``schema``.

        Depth-bounded: a self-referential schema would otherwise recurse forever.
        """
        if depth > 6:
            return None

        # enum / const win outright - they are the tightest constraint available.
        if "const" in schema:
            return schema["const"]
        if enum := schema.get("enum"):
            concrete = [value for value in enum if value is not None]
            if not concrete:
                return None
            return concrete[seed % len(concrete)]

        if "anyOf" in schema or "oneOf" in schema:
            branches = schema.get("anyOf") or schema.get("oneOf") or []
            for branch in branches:
                if branch.get("type") != "null":
                    return self._from_schema(
                        branch, seed=seed, prompt=prompt, key=key, depth=depth + 1
                    )
            return None

        declared = schema.get("type")
        types = declared if isinstance(declared, list) else [declared]
        # Prefer a concrete type over null so generated payloads are populated.
        chosen = next((t for t in types if t and t != "null"), None)

        if chosen == "object":
            result: dict[str, Any] = {}
            properties: dict[str, Any] = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            for index, (name, subschema) in enumerate(properties.items()):
                # Populate required fields plus a deterministic subset of the rest,
                # so optional-field handling downstream is genuinely exercised.
                if name not in required and (seed + index) % 3 == 2:
                    continue
                result[name] = self._from_schema(
                    subschema, seed=seed + index, prompt=prompt, key=name, depth=depth + 1
                )
            return result

        if chosen == "array":
            items = schema.get("items") or {}
            count = 1 + (seed % 3)
            return [
                self._from_schema(
                    items, seed=seed + offset, prompt=prompt, key=key, depth=depth + 1
                )
                for offset in range(count)
            ]

        if chosen == "boolean":
            return bool(seed % 2)
        if chosen == "integer":
            return self._hinted(key, seed, integral=True)
        if chosen == "number":
            return self._hinted(key, seed, integral=False)
        if chosen == "string":
            # Grounded fields are answered from the prompt, not invented. Without
            # this the stub behaves like a fabricating model: extraction validation
            # rejects every citation and every quote, and the pipeline can never be
            # exercised end to end. Fabrication is tested deliberately elsewhere by
            # feeding the validator text that is genuinely absent from the evidence.
            if key in _EVIDENCE_ID_KEYS:
                return self._evidence_id(prompt, seed)
            if key in _VERBATIM_KEYS:
                return self._evidence_quote(prompt, seed)
            return self._string_for(key, schema, seed)
        return None

    # ------------------------------------------------------- grounded generation
    @staticmethod
    def _evidence_blocks(prompt: str) -> list[tuple[str, str]]:
        """Parse ``[id] ... text`` blocks back out of a rendered evidence bundle.

        The stub reads the same labels a real model is asked to cite, so its output
        is checkable by exactly the validation a real response goes through.
        """
        blocks: list[tuple[str, str]] = []
        for match in _EVIDENCE_BLOCK.finditer(prompt):
            identifier = match.group("id")
            body = match.group("body").strip()
            if identifier and body:
                blocks.append((identifier, body))
        return blocks

    def _evidence_id(self, prompt: str, seed: int) -> str:
        blocks = self._evidence_blocks(prompt)
        if not blocks:
            return ""
        return blocks[seed % len(blocks)][0]

    def _evidence_quote(self, prompt: str, seed: int) -> str:
        """A real sentence from the supplied evidence, so quote checks can pass."""
        blocks = self._evidence_blocks(prompt)
        if not blocks:
            return ""
        _, body = blocks[seed % len(blocks)]
        # Drop the header line the renderer added; quote the body itself.
        lines = [line for line in body.splitlines() if line.strip()]
        text = "\n".join(lines[1:]) if len(lines) > 1 else body
        sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
        if not sentences:
            return text[:400]
        start = seed % len(sentences)
        return " ".join(sentences[start : start + 2])[:600]

    @staticmethod
    def _hinted(key: str, seed: int, *, integral: bool) -> Any:
        lowered = key.lower()
        for fragments, value in _HINTS:
            if any(fragment in lowered for fragment in fragments):
                if isinstance(value, str):
                    continue
                return int(value) if integral else float(value)
        return (seed % 90) + 10 if integral else round((seed % 900) / 10 + 1, 2)

    @staticmethod
    def _string_for(key: str, schema: dict[str, Any], seed: int) -> str:
        fmt = schema.get("format")
        if fmt == "date":
            return "2026-01-01"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "email":
            return "legal@example.com"
        if fmt == "uri":
            return "https://example.com/contract"

        lowered = key.lower()
        for fragments, value in _HINTS:
            if isinstance(value, str) and any(f in lowered for f in fragments):
                return value
        return f"mock value for {key or 'field'}"


__all__ = ["MockInferenceProvider"]

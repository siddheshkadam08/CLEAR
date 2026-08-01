"""Embedding compatibility validation.

Cosine similarity is only meaningful between vectors from the *same* embedding
model. Two models trained separately produce coordinates in unrelated spaces, so
a distance computed across them is arithmetic without semantics - it returns a
number, ranks results by it, and is wrong with no error anywhere.

That failure mode is the reason this module exists rather than a comment. The
schema already records provenance on every row (`provider`, `model`, `dim`,
`embedding_version`, `strategy_version`); nothing enforced it, so a provider
switch silently interleaved two spaces in one index and degraded retrieval for
every query afterwards.

Two checks, applied at different moments:

* :meth:`EmbeddingValidator.validate_batch` - before an insert. A row that does
  not match the active configuration is rejected outright.
* :meth:`EmbeddingValidator.audit_rows` - against what is already stored, via
  ``EmbeddingRepository.space_census``. Answers "is this index internally
  consistent, and if not what has to be re-indexed", which is what the reindex
  command needs to plan its work.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.errors import ValidationError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class EmbeddingSpace:
    """The identity of a vector space.

    Two vectors are comparable exactly when their spaces are equal. Model and
    dimension are obvious; ``embedding_version`` and ``strategy_version`` are
    included because the *composed input* matters as much as the model - changing
    what text is embedded (adding the title, changing the prefix) moves vectors
    without changing the model name.
    """

    provider: str
    model: str
    dim: int
    embedding_version: str
    strategy_version: str

    @classmethod
    def active(cls) -> EmbeddingSpace:
        """The space the current configuration actually produces.

        Resolved through the *provider*, not straight from settings, because the
        provider is what stamps the row and it does not always echo the configured
        name back: the mock provider prefixes ``mock-`` precisely so a fake vector
        can never be mistaken for a real one. Comparing rows against the raw
        setting would reject every legitimate row on any such provider.

        Falls back to settings if the provider cannot be constructed - validation
        is a guard rail, and it must not be the thing that takes a process down.
        """
        from app.core.versions import EMBEDDING_STRATEGY_VERSION

        settings = get_settings().embedding
        provider_name: str = settings.provider
        model = settings.model
        try:
            from app.ai.embedding.providers import get_embedding_provider

            provider = get_embedding_provider()
            provider_name, model = provider.name, provider.model
        except Exception as exc:  # noqa: BLE001 - degrade to configured values
            logger.debug("embedding_space_provider_unavailable", error=str(exc))

        return cls(
            provider=provider_name,
            model=model,
            dim=int(settings.dim),
            embedding_version=settings.version,
            strategy_version=EMBEDDING_STRATEGY_VERSION,
        )

    @classmethod
    def from_row(cls, row: dict[str, Any] | Any) -> EmbeddingSpace:
        get = row.get if isinstance(row, dict) else lambda k, d=None: getattr(row, k, d)
        return cls(
            provider=str(get("provider", "") or ""),
            model=str(get("model", "") or ""),
            dim=int(get("dim", 0) or 0),
            embedding_version=str(get("embedding_version", "") or ""),
            strategy_version=str(get("strategy_version", "") or ""),
        )

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}@{self.dim}/{self.embedding_version}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "dim": self.dim,
            "embedding_version": self.embedding_version,
            "strategy_version": self.strategy_version,
        }


@dataclass(slots=True)
class ValidationReport:
    """Outcome of validating a batch."""

    accepted: int = 0
    rejected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    rejected_samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.rejected == 0

    def _reject(self, reason: str, detail: dict[str, Any]) -> None:
        self.rejected += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1
        if len(self.rejected_samples) < 5:
            self.rejected_samples.append({"reason": reason, **detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "reasons": self.reasons,
            "samples": self.rejected_samples,
        }


@dataclass(slots=True)
class StoreAudit:
    """What is actually in the vector store, grouped by space."""

    active: EmbeddingSpace
    spaces: dict[str, int] = field(default_factory=dict)
    incompatible_rows: int = 0

    @property
    def is_consistent(self) -> bool:
        """True when every stored vector shares the active space."""
        return self.incompatible_rows == 0

    @property
    def foreign_spaces(self) -> list[str]:
        return sorted(label for label in self.spaces if label != self.active.label)

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_space": self.active.label,
            "spaces": self.spaces,
            "incompatible_rows": self.incompatible_rows,
            "consistent": self.is_consistent,
            "foreign_spaces": self.foreign_spaces,
        }


class EmbeddingValidator:
    """Rejects vectors that cannot be compared with the active space."""

    def __init__(self, expected: EmbeddingSpace | None = None) -> None:
        self.expected = expected or EmbeddingSpace.active()

    # --------------------------------------------------------------- pre-insert
    def validate_row(self, row: dict[str, Any], report: ValidationReport) -> bool:
        """Check one prospective row. Returns True when it may be inserted."""
        space = EmbeddingSpace.from_row(row)

        if space.model != self.expected.model or space.provider != self.expected.provider:
            report._reject(
                "foreign_space",
                {"found": space.label, "expected": self.expected.label},
            )
            return False

        if space.dim != self.expected.dim:
            report._reject(
                "dimension_mismatch",
                {"found": space.dim, "expected": self.expected.dim},
            )
            return False

        if space.embedding_version != self.expected.embedding_version:
            report._reject(
                "version_mismatch",
                {
                    "found": space.embedding_version,
                    "expected": self.expected.embedding_version,
                },
            )
            return False

        vector = row.get("embedding")
        if vector is None:
            report._reject("missing_vector", {"ref_id": str(row.get("ref_id"))})
            return False

        # A length check on the payload itself, not only the declared `dim`: the
        # declared value is metadata the caller supplied, while this is the thing
        # that will actually be indexed. They diverge when a provider silently
        # truncates.
        if isinstance(vector, list | tuple) and len(vector) != self.expected.dim:
            report._reject(
                "vector_length_mismatch",
                {"found": len(vector), "expected": self.expected.dim},
            )
            return False

        report.accepted += 1
        return True

    def validate_batch(
        self, rows: Sequence[dict[str, Any]], *, strict: bool = True
    ) -> tuple[list[dict[str, Any]], ValidationReport]:
        """Split ``rows`` into insertable rows and a report.

        ``strict`` raises instead of returning a partial batch. Indexing uses
        strict mode: a contract that is half-indexed in the wrong space is worse
        than one that failed, because it answers queries and looks fine.
        """
        report = ValidationReport()
        keep = [row for row in rows if self.validate_row(row, report)]

        if report.rejected:
            logger.error(
                "embedding_validation_rejected",
                expected=self.expected.label,
                **report.as_dict(),
            )
            if strict:
                raise ValidationError(
                    f"{report.rejected} embedding(s) are not compatible with the active "
                    f"space ({self.expected.label}). Mixing embedding spaces silently "
                    "corrupts similarity search, so the batch was refused.",
                    details=report.as_dict(),
                )
        return keep, report

    # ------------------------------------------------------------- store audit
    def audit_rows(self, rows: Iterable[dict[str, Any] | Any]) -> StoreAudit:
        """Group stored rows by space and count what cannot be compared."""
        audit = StoreAudit(active=self.expected)
        for row in rows:
            space = EmbeddingSpace.from_row(row)
            count = int(
                (row.get("count", 1) if isinstance(row, dict) else getattr(row, "count", 1)) or 1
            )
            audit.spaces[space.label] = audit.spaces.get(space.label, 0) + count
            if space.label != self.expected.label:
                audit.incompatible_rows += count
        return audit


__all__ = [
    "EmbeddingSpace",
    "EmbeddingValidator",
    "StoreAudit",
    "ValidationReport",
]

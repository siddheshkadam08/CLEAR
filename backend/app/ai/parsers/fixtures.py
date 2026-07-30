"""Record and replay parser responses (``PARSER_MODE``).

The layout service is rate-limited and slow, and a test suite that calls it is a
test suite nobody runs offline. So every parser response is recorded on the way
through, keyed by the document's SHA-256, and replayed from disk in ``fixture``
mode.

Keyed by content hash rather than by file name or contract id on purpose:

* the same PDF uploaded twice, under two names, into two projects, replays the
  same fixture - which is also what makes fixture mode *deterministic*;
* a changed file gets a different key, so a stale fixture can never silently
  stand in for a document it was not produced from.

Recording happens at the **raw vendor payload** level, not at the
:class:`~app.ai.cdm.NormalizedDocument` level. That is deliberate: replaying the
raw payload keeps the adapter's own mapping code - the part most likely to have
bugs - on the execution path during tests. Replaying a finished NormalizedDocument
would test nothing but the fixture loader.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.errors import ParserError
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Used when no fixture matches the document's hash. Lets a developer drop in one
#: sample response and have *any* upload work offline, which is what makes local
#: UI work possible without a parser account.
DEFAULT_FIXTURE = "default.json"


@dataclass(slots=True)
class FixtureRecord:
    """A stored parser response and the provenance needed to trust it."""

    payloads: list[dict[str, Any]]
    parser: str
    file_hash: str
    file_name: str | None = None
    recorded_at: str | None = None
    source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "parser": self.parser,
            "file_hash": self.file_hash,
            "file_name": self.file_name,
            "recorded_at": self.recorded_at,
            "source": self.source,
            "payloads": self.payloads,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FixtureRecord:
        payloads = raw.get("payloads")
        if not isinstance(payloads, list):
            raise ValueError("fixture has no 'payloads' array")
        return cls(
            payloads=payloads,
            parser=str(raw.get("parser") or "unknown"),
            file_hash=str(raw.get("file_hash") or ""),
            file_name=raw.get("file_name"),
            recorded_at=raw.get("recorded_at"),
            source=raw.get("source"),
        )


class FixtureStore:
    """Reads and writes parser fixtures on the local filesystem."""

    def __init__(self, directory: Path | str | None = None, *, parser: str = "idoc") -> None:
        settings = get_settings().parser
        self.parser = parser
        self.directory = Path(directory or settings.fixture_dir) / parser

    # ------------------------------------------------------------------ paths
    def path_for(self, file_hash: str) -> Path:
        # Truncated to 32 hex chars: still far beyond collision risk for a fixture
        # set, and short enough that the directory stays readable.
        return self.directory / f"{file_hash[:32]}.json"

    @property
    def default_path(self) -> Path:
        return self.directory / DEFAULT_FIXTURE

    # ------------------------------------------------------------------- read
    def load(self, file_hash: str) -> FixtureRecord | None:
        """The fixture for this exact document, or ``None``."""
        return self._read(self.path_for(file_hash))

    def load_default(self) -> FixtureRecord | None:
        """The fallback fixture, or ``None``."""
        return self._read(self.default_path)

    def _read(self, path: Path) -> FixtureRecord | None:
        if not path.is_file():
            return None
        try:
            return FixtureRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # A corrupt fixture must not masquerade as "no fixture" - that would
            # send the caller to the fallback and quietly parse the wrong document.
            raise ParserError(
                f"The parser fixture at {path} could not be read: {exc}",
                retryable=False,
                details={"path": str(path)},
            ) from exc

    def available(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(p.name for p in self.directory.glob("*.json"))

    # ------------------------------------------------------------------ write
    def save(
        self,
        file_hash: str,
        payloads: list[dict[str, Any]],
        *,
        file_name: str | None = None,
        source: str | None = None,
        make_default: bool | None = None,
    ) -> Path:
        """Record a response. Never overwrites an existing fixture.

        Not overwriting is what keeps replay deterministic: once a hash has a
        recorded response, every later run - live or fixture - sees the same bytes,
        so a service that starts returning something different cannot silently
        change what the tests assert against.
        """
        path = self.path_for(file_hash)
        if path.exists():
            return path

        record = FixtureRecord(
            payloads=payloads,
            parser=self.parser,
            file_hash=file_hash,
            file_name=file_name,
            recorded_at=datetime.now(UTC).isoformat(),
            source=source,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record.as_dict(), indent=2), encoding="utf-8")

        # The first recorded response also becomes the fallback, so a fresh clone
        # that runs one live parse is immediately able to work offline.
        if make_default is None:
            make_default = not self.default_path.exists()
        if make_default:
            self.default_path.write_text(json.dumps(record.as_dict(), indent=2), encoding="utf-8")

        logger.info(
            "parser_fixture_recorded",
            parser=self.parser,
            file_hash=file_hash[:16],
            pages=len(payloads),
            path=str(path),
        )
        return path


def resolve(
    store: FixtureStore, file_hash: str, *, file_name: str | None = None
) -> list[dict[str, Any]]:
    """The payloads to replay, or an actionable error explaining what is missing.

    Falling back to the default fixture is logged loudly rather than silently: the
    replayed document is *not* the one that was uploaded, and a developer looking
    at extracted clauses that do not match their PDF needs that stated, not
    inferred.
    """
    record = store.load(file_hash)
    if record is not None:
        logger.info(
            "parser_fixture_replayed",
            parser=store.parser,
            file_hash=file_hash[:16],
            pages=len(record.payloads),
        )
        return record.payloads

    fallback = store.load_default()
    if fallback is not None:
        logger.warning(
            "parser_fixture_fallback",
            parser=store.parser,
            requested_hash=file_hash[:16],
            file_name=file_name,
            detail=(
                "No fixture recorded for this document; replaying the default "
                "sample instead. Extracted content will describe the sample, not "
                "the uploaded file."
            ),
        )
        return fallback.payloads

    raise ParserError(
        f"PARSER_MODE=fixture but no recorded response exists for this document "
        f"(hash {file_hash[:16]}), and no {DEFAULT_FIXTURE} is present in "
        f"{store.directory}. Record one with PARSER_MODE=live, or add a sample "
        f"fixture for offline development.",
        retryable=False,
        details={
            "file_hash": file_hash,
            "fixture_dir": str(store.directory),
            "available": store.available()[:20],
        },
    )


__all__ = ["DEFAULT_FIXTURE", "FixtureRecord", "FixtureStore", "resolve"]

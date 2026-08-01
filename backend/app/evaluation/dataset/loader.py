"""Loading, validating and discovering golden datasets.

Two file formats, for two different sizes of dataset:

* **JSON** - one object with metadata and a ``cases`` array. Readable, diffable,
  and what a hand-written set of fifty questions should be.
* **JSONL** - one case per line, with an optional leading metadata line. The
  format that scales: ten thousand cases in a JSON array is a file no reviewer
  can diff and no editor will open, while JSONL diffs per case and streams.

Validation is strict and happens at load. A benchmark that discovers a malformed
case halfway through a forty-minute run has wasted the run, and - worse - a
silently skipped case makes the denominator wrong, which quietly moves every
metric in the direction of whichever cases survived.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.evaluation.dataset.models import SCHEMA_VERSION, GoldenCase, GoldenDataset

logger = get_logger(__name__)

#: Where datasets shipped with the repository live.
GOLDEN_ROOT = Path(__file__).resolve().parent.parent / "golden"


class DatasetError(ValueError):
    """A dataset that cannot be trusted to produce a meaningful score."""


def load_dataset(source: str | Path) -> GoldenDataset:
    """Load a dataset by path, or by name from the bundled ``golden`` directory.

    >>> load_dataset("banking-msa")          # resolves golden/banking-msa.jsonl
    >>> load_dataset("/data/custom.json")    # or an explicit path
    """
    path = _resolve(source)
    dataset = _load_jsonl(path) if path.suffix == ".jsonl" else _load_json(path)
    _validate(dataset, path)
    logger.info(
        "golden_dataset_loaded",
        dataset=dataset.identifier,
        path=str(path),
        **{k: v for k, v in dataset.statistics().items() if k not in {"name", "version", "tags"}},
    )
    return dataset


def available_datasets() -> list[str]:
    """Every dataset name the bundled directory offers."""
    if not GOLDEN_ROOT.exists():
        return []
    return sorted(
        {
            path.stem
            for path in GOLDEN_ROOT.iterdir()
            if path.suffix in {".json", ".jsonl"} and not path.name.startswith("_")
        }
    )


def write_dataset(dataset: GoldenDataset, path: str | Path) -> Path:
    """Write a dataset out, choosing the format from the extension.

    Round-trips: ``load_dataset(write_dataset(d, p)) == d`` in content. That
    property is what makes the ``dataset generate`` bootstrap usable - a
    generated skeleton can be edited by hand and re-loaded without a conversion
    step in between.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.suffix == ".jsonl":
        lines = [
            json.dumps(
                {
                    "_meta": {
                        "name": dataset.name,
                        "version": dataset.version,
                        "description": dataset.description,
                        "schemaVersion": dataset.schema_version,
                        "tags": dataset.tags,
                    }
                }
            )
        ]
        lines.extend(json.dumps(case.as_dict(), ensure_ascii=False) for case in dataset.cases)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        target.write_text(
            json.dumps(dataset.as_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return target


# =============================================================================
# Internals
# =============================================================================
def _resolve(source: str | Path) -> Path:
    candidate = Path(source)
    if candidate.exists():
        return candidate

    for suffix in (".jsonl", ".json"):
        bundled = GOLDEN_ROOT / f"{source}{suffix}"
        if bundled.exists():
            return bundled

    known = ", ".join(available_datasets()) or "none are bundled"
    raise DatasetError(f"No dataset '{source}'. Available: {known}.")


def _load_json(path: Path) -> GoldenDataset:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path.name} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise DatasetError(f"{path.name} must hold an object with a 'cases' array.")

    return GoldenDataset(
        name=str(payload.get("name") or path.stem),
        version=str(payload.get("version") or "1"),
        description=str(payload.get("description") or ""),
        schema_version=int(payload.get("schemaVersion") or SCHEMA_VERSION),
        tags=[str(tag) for tag in (payload.get("tags") or [])],
        cases=_parse_cases(payload.get("cases") or [], path),
    )


def _load_jsonl(path: Path) -> GoldenDataset:
    meta: dict[str, Any] = {}
    raw_cases: list[tuple[int, dict[str, Any]]] = []

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{path.name} line {number} is not valid JSON: {exc}") from exc

        if isinstance(record, dict) and "_meta" in record:
            meta = record["_meta"] or {}
            continue
        raw_cases.append((number, record))

    cases: list[GoldenCase] = []
    for number, record in raw_cases:
        try:
            cases.append(GoldenCase.from_dict(record))
        except (ValueError, TypeError) as exc:
            raise DatasetError(f"{path.name} line {number}: {exc}") from exc

    return GoldenDataset(
        name=str(meta.get("name") or path.stem),
        version=str(meta.get("version") or "1"),
        description=str(meta.get("description") or ""),
        schema_version=int(meta.get("schemaVersion") or SCHEMA_VERSION),
        tags=[str(tag) for tag in (meta.get("tags") or [])],
        cases=cases,
    )


def _parse_cases(records: Any, path: Path) -> list[GoldenCase]:
    if not isinstance(records, list):
        raise DatasetError(f"{path.name}: 'cases' must be an array.")

    cases: list[GoldenCase] = []
    for index, record in enumerate(records):
        try:
            cases.append(GoldenCase.from_dict(record))
        except (ValueError, TypeError, AttributeError) as exc:
            raise DatasetError(f"{path.name} case {index}: {exc}") from exc
    return cases


def _validate(dataset: GoldenDataset, path: Path) -> None:
    if dataset.schema_version > SCHEMA_VERSION:
        raise DatasetError(
            f"{path.name} declares schema version {dataset.schema_version}, but this "
            f"build understands {SCHEMA_VERSION}. Upgrade rather than reading it "
            "partially - a half-understood case scores as though its expectations "
            "were absent."
        )

    if not dataset.cases:
        raise DatasetError(f"{path.name} contains no cases.")

    seen: set[str] = set()
    duplicates: set[str] = set()
    for case in dataset.cases:
        if case.id in seen:
            duplicates.add(case.id)
        seen.add(case.id)

    if duplicates:
        # Duplicate ids make per-case results collide, so a regression report
        # would silently show one result where two runs happened.
        raise DatasetError(
            f"{path.name} has duplicate case ids: {', '.join(sorted(duplicates)[:5])}."
        )

    scoreable = sum(1 for case in dataset.cases if case.expected.has_relevance_signal)
    if not scoreable:
        logger.warning(
            "golden_dataset_has_no_retrieval_expectations",
            dataset=dataset.identifier,
            detail="retrieval metrics will be empty; only guardrail and latency are scored",
        )


__all__ = [
    "GOLDEN_ROOT",
    "DatasetError",
    "available_datasets",
    "load_dataset",
    "write_dataset",
]

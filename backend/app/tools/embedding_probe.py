"""Probe the configured embedding provider and report what it actually returns.

    python -m app.tools.embedding_probe
    python -m app.tools.embedding_probe --json
    python -m app.tools.embedding_probe --expect-dim 2048

The dimension is read from the provider's response, never from configuration. That
is the entire point: ``EMBEDDING_DIM`` is what this deployment *believes*, and this
tool exists to find out whether the belief is true before a contract is uploaded
against it.

Exit codes: 0 healthy, 1 the provider failed or disagreed with the configuration,
3 the tool could not run.
"""

from __future__ import annotations

import argparse
import statistics
import time
from typing import Any

from app.tools._common import (
    EXIT_FAILED,
    EXIT_OK,
    EXIT_UNUSABLE,
    base_parser,
    configure_tool_logging,
    dim,
    emit,
    heading,
    mark,
    run,
)
from app.tools._common import (
    base_parser as _base,  # noqa: F401 - re-exported for tests
)

SAMPLE = "This Agreement shall be governed by the laws of England and Wales."


async def probe(args: argparse.Namespace) -> int:
    from app.ai.embedding import get_embedding_provider
    from app.core.config import get_settings

    configure_tool_logging(args.as_json)
    settings = get_settings().embedding

    report: dict[str, Any] = {
        "provider": settings.provider,
        "model": settings.model,
        "endpoint": settings.nvidia_base_url if settings.provider == "nvidia" else None,
        "configured_dimension": settings.dim,
        "storage": settings.storage,
        "authenticated": bool(settings.nvidia_api_key) if settings.provider == "nvidia" else None,
        "status": "unknown",
        "dimension": None,
        "latency_ms": None,
        "latencies_ms": [],
        "normalised": None,
        "error": None,
    }

    try:
        provider = get_embedding_provider()
    except Exception as exc:  # noqa: BLE001 - reporting the failure is the job
        report["status"] = "unusable"
        report["error"] = str(exc)[:400]
        emit(report, lambda: _render(report, []), as_json=args.as_json)
        return EXIT_UNUSABLE

    latencies: list[int] = []
    vector: list[float] | None = None
    for attempt in range(max(1, args.samples)):
        started = time.perf_counter()
        try:
            # A real sentence, embedded as a *query*, because that is the path most
            # likely to be misconfigured: the prefix only applies on this side.
            vector = await provider.embed_query(f"{SAMPLE} ({attempt})")
        except Exception as exc:  # noqa: BLE001
            report["status"] = "error"
            report["error"] = str(exc)[:400]
            report["latencies_ms"] = latencies
            emit(report, lambda: _render(report, []), as_json=args.as_json)
            return EXIT_FAILED
        latencies.append(int((time.perf_counter() - started) * 1000))

    assert vector is not None
    magnitude = sum(value * value for value in vector) ** 0.5

    report["dimension"] = len(vector)
    report["latency_ms"] = int(statistics.median(latencies))
    report["latencies_ms"] = latencies
    report["normalised"] = abs(magnitude - 1.0) < 1e-3
    report["status"] = "healthy"

    problems: list[str] = []
    expected = args.expect_dim or settings.dim
    if len(vector) != expected:
        problems.append(
            f"The provider returned {len(vector)} dimensions but "
            f"{'--expect-dim' if args.expect_dim else 'EMBEDDING_DIM'} is {expected}. "
            f"Set EMBEDDING_DIM={len(vector)}, migrate, and re-index. Nothing is "
            "truncated or padded to hide this."
        )
    if not report["normalised"]:
        problems.append(
            f"Vectors are not unit length (magnitude {magnitude:.4f}). Cosine distance "
            "and the HNSW indexes assume they are."
        )

    from app.ai.embedding.pgvector import HNSW_MAX_DIMS

    ceiling = HNSW_MAX_DIMS.get(settings.storage, 0)
    if len(vector) > ceiling:
        problems.append(
            f"{len(vector)} dimensions cannot be HNSW-indexed on "
            f"EMBEDDING_STORAGE={settings.storage} (limit {ceiling}). Use halfvec, or "
            "reduce EMBEDDING_DIM."
        )

    report["problems"] = problems
    if problems:
        report["status"] = "misconfigured"

    emit(report, lambda: _render(report, problems), as_json=args.as_json)
    return EXIT_FAILED if problems else EXIT_OK


def _render(report: dict[str, Any], problems: list[str]) -> None:
    print(heading("Embedding provider probe"))
    rows = [
        ("Provider", report["provider"]),
        ("Model", report["model"]),
        ("Endpoint", report["endpoint"] or "(local)"),
        ("Authenticated", _tri(report["authenticated"])),
        ("Dimension", report["dimension"] if report["dimension"] else "-"),
        ("Configured", report["configured_dimension"]),
        ("Storage", report["storage"]),
        ("Latency", f"{report['latency_ms']} ms" if report["latency_ms"] else "-"),
        ("Unit length", _tri(report["normalised"])),
        ("Status", report["status"]),
    ]
    for label, value in rows:
        print(f"  {label:<15} {value}")

    if report["latencies_ms"] and len(report["latencies_ms"]) > 1:
        samples = ", ".join(f"{value} ms" for value in report["latencies_ms"])
        print(dim(f"  {'Samples':<15} {samples}"))

    if report["error"]:
        print(f"\n{mark(False)} {report['error']}")
    for problem in problems:
        print(f"\n{mark(False)} {problem}")
    if not problems and not report["error"]:
        print(f"\n{mark(True)} The provider is reachable and matches the configuration.")


def _tri(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def build_parser() -> argparse.ArgumentParser:
    parser = base_parser(__doc__ or "")
    parser.add_argument(
        "--expect-dim",
        type=int,
        default=None,
        help="Fail unless the provider returns exactly this many dimensions.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help="Embed this many times and report the median latency.",
    )
    return parser


def main() -> int:
    return run(probe, build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())

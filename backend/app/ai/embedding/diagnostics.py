"""Embedding configuration diagnostics - fail at startup, not at first upload.

A broken embedding configuration is uniquely bad at hiding. Upload, validation,
parsing, enrichment, classification and chunking all succeed; the failure lands on
stage seven of eight, minutes later, on a background worker, in a job the user is
watching a progress bar for. Worse failures do not fail at all: a wrong
``EMBEDDING_DIM`` against an existing column, or a 2048-dimension model on a column
whose HNSW index silently does not exist, degrade quality rather than raising.

So everything checkable is checked while the process is still starting:

* credentials present for the selected provider
* the provider answers, and answers in the width it is configured for
* the database column is the width the provider returns
* the column can actually carry an HNSW index at that width

Each check reports rather than raises; :func:`assert_ready` decides what is fatal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: pgvector's HNSW ceiling per column type. Exceeding it does not fail the insert -
#: it fails index *creation*, which is easy to miss in a migration log and leaves
#: every similarity query doing a sequential scan.
HNSW_MAX_DIMS: dict[str, int] = {"vector": 2000, "halfvec": 4000}


@dataclass(slots=True)
class Finding:
    """One diagnostic outcome."""

    check: str
    ok: bool
    detail: str
    fatal: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"check": self.check, "ok": self.ok, "detail": self.detail, "fatal": self.fatal}


@dataclass(slots=True)
class EmbeddingDiagnostics:
    """The full picture, for startup logging and the health endpoint."""

    provider: str
    model: str
    configured_dim: int
    storage: str
    native_dim: int | None = None
    reported_dim: int | None = None
    column_dim: int | None = None
    column_type: str | None = None
    latency_ms: int | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(finding.ok for finding in self.findings)

    @property
    def fatal_problems(self) -> list[Finding]:
        return [f for f in self.findings if not f.ok and f.fatal]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "configured_dim": self.configured_dim,
            "native_dim": self.native_dim,
            "reported_dim": self.reported_dim,
            "column_dim": self.column_dim,
            "column_type": self.column_type,
            "storage": self.storage,
            "latency_ms": self.latency_ms,
            "ok": self.ok,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def check_configuration(settings: Settings | None = None) -> list[Finding]:
    """Static checks. No network, no database - safe to run anywhere."""
    resolved = settings or get_settings()
    embedding = resolved.embedding
    findings: list[Finding] = []

    if embedding.provider == "nvidia":
        if not embedding.nvidia_api_key:
            findings.append(
                Finding(
                    check="credentials",
                    ok=False,
                    fatal=True,
                    detail=(
                        "EMBEDDING_PROVIDER=nvidia but NVIDIA_API_KEY is empty. A "
                        "self-hosted NIM without auth can set it to any non-empty "
                        "placeholder."
                    ),
                )
            )
        else:
            findings.append(Finding("credentials", True, "NVIDIA_API_KEY is set."))

        if not embedding.nvidia_base_url:
            findings.append(
                Finding(
                    check="endpoint",
                    ok=False,
                    fatal=True,
                    detail="NVIDIA_BASE_URL is empty.",
                )
            )
        elif not embedding.nvidia_base_url.startswith(("http://", "https://")):
            findings.append(
                Finding(
                    check="endpoint",
                    ok=False,
                    fatal=True,
                    detail=f"NVIDIA_BASE_URL is not a URL: {embedding.nvidia_base_url!r}",
                )
            )
        else:
            findings.append(Finding("endpoint", True, embedding.nvidia_base_url))

        if not embedding.nvidia_query_prefix or not embedding.nvidia_passage_prefix:
            # Not fatal - a future symmetric NIM model would legitimately want them
            # blank - but worth saying out loud, because silently losing the prefixes
            # on an asymmetric model costs recall with nothing in the logs.
            findings.append(
                Finding(
                    check="input_prefixes",
                    ok=True,
                    detail=(
                        "Query/passage prefixes are disabled. Nemotron 3 Embed is "
                        "asymmetric and expects them; recall will suffer if this is "
                        "not deliberate."
                    ),
                )
            )

    if embedding.provider in {"openai", "azure_openai"}:
        findings.append(
            Finding(
                check="credentials",
                ok=bool(resolved.llm.openai_api_key or resolved.llm.azure_openai_api_key),
                fatal=True,
                detail="OPENAI_API_KEY / AZURE_OPENAI_API_KEY for the selected provider.",
            )
        )

    # Dimension vs the model's published width.
    native = embedding.native_dim
    if embedding.dim > native:
        findings.append(
            Finding(
                check="dimension",
                ok=False,
                fatal=True,
                detail=(
                    f"EMBEDDING_DIM={embedding.dim} exceeds the {native} dimensions "
                    f"{embedding.model} produces. Vectors are never padded to fit."
                ),
            )
        )
    elif embedding.dim < native:
        findings.append(
            Finding(
                check="dimension",
                ok=True,
                detail=(
                    f"EMBEDDING_DIM={embedding.dim} truncates {embedding.model}'s "
                    f"{native} dimensions (Matryoshka). Vectors are re-normalised "
                    "after slicing."
                ),
            )
        )
    else:
        findings.append(Finding("dimension", True, f"{embedding.dim} matches {embedding.model}."))

    # The check that is easiest to miss and most expensive to discover late.
    ceiling = HNSW_MAX_DIMS.get(embedding.storage, 0)
    if embedding.dim > ceiling:
        findings.append(
            Finding(
                check="index_capability",
                ok=False,
                fatal=True,
                detail=(
                    f"EMBEDDING_STORAGE={embedding.storage} can carry an HNSW index up "
                    f"to {ceiling} dimensions, but EMBEDDING_DIM is {embedding.dim}. "
                    "The column would be created and then every similarity search "
                    "would fall back to a sequential scan. Use EMBEDDING_STORAGE="
                    "halfvec, or reduce EMBEDDING_DIM."
                ),
            )
        )
    else:
        findings.append(
            Finding(
                "index_capability",
                True,
                f"{embedding.storage}({embedding.dim}) is HNSW-indexable (limit {ceiling}).",
            )
        )

    return findings


async def check_provider(
    settings: Settings | None = None,
) -> tuple[list[Finding], int | None, int | None]:
    """Round-trip the provider. Returns findings, the reported width and latency."""
    from app.ai.embedding import get_embedding_provider

    resolved = settings or get_settings()
    provider = get_embedding_provider()
    probe = await provider.probe()

    if not probe.ok:
        return (
            [
                Finding(
                    check="connectivity",
                    ok=False,
                    fatal=True,
                    detail=f"The embedding provider did not answer: {probe.error}",
                )
            ],
            None,
            probe.latency_ms,
        )

    findings = [Finding("connectivity", True, f"Answered in {probe.latency_ms} ms.")]
    configured = resolved.embedding.dim
    if probe.dim != configured:
        findings.append(
            Finding(
                check="reported_dimension",
                ok=False,
                fatal=True,
                detail=(
                    f"{resolved.embedding.model} returned {probe.dim} dimensions but "
                    f"EMBEDDING_DIM is {configured}. Set EMBEDDING_DIM to {probe.dim} "
                    "and re-embed; nothing is truncated or padded to hide this."
                ),
            )
        )
    else:
        findings.append(Finding("reported_dimension", True, f"Returned {probe.dim} dimensions."))
    return findings, probe.dim, probe.latency_ms


async def check_database(
    db: AsyncSession, settings: Settings | None = None
) -> tuple[list[Finding], int | None, str | None]:
    """Compare the live ``embeddings.embedding`` column against the configuration.

    Reads the column's actual type and width from ``information_schema`` /
    ``pg_attribute`` rather than trusting the migration to have been applied. A
    schema one migration behind is the single most common cause of "search returns
    nothing" after an embedding-model change.

    The lookup is schema-qualified, for the same reason the metadata carries a
    schema (see :func:`app.db.base._configured_schema`): ``embeddings`` routinely
    exists in more than one schema of a shared database - ``public`` left by an
    earlier deployment, plus the configured ``DB_SCHEMA``. Matching on the table
    name alone found both, and ``scalar_one_or_none`` turned that into
    "Multiple rows were found when one or none was required" - reported as a
    warning, which meant the two *fatal* checks below never ran at all. A column
    whose width or type disagrees with the configuration would then have started
    the API cleanly, which is precisely what those checks exist to prevent.
    """
    resolved = settings or get_settings()
    findings: list[Finding] = []

    # `to_regclass` resolves the name exactly as the rest of the app does: the
    # configured schema when there is one, otherwise the connection's
    # search_path. It returns NULL rather than raising when nothing matches, so a
    # missing table still falls through to the "run migrations" finding below.
    schema = resolved.db.schema_name.strip()
    qualified_table = f"{schema}.embeddings" if schema else "embeddings"

    try:
        row = (
            await db.execute(
                text(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod) AS coltype
                    FROM pg_attribute a
                    WHERE a.attrelid = to_regclass(:table)
                      AND a.attname = 'embedding'
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    """
                ),
                {"table": qualified_table},
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - a DB that is not up yet is not a config error
        return (
            [Finding("database", False, f"Could not inspect the embeddings table: {exc}")],
            None,
            None,
        )

    if row is None:
        return (
            [
                Finding(
                    check="database",
                    ok=False,
                    fatal=False,
                    detail=(
                        "The embeddings table does not exist yet. Run migrations before ingesting."
                    ),
                )
            ],
            None,
            None,
        )

    # `halfvec(2048)` / `vector(1536)`
    coltype = str(row)
    kind = coltype.split("(", 1)[0].strip()
    width: int | None = None
    if "(" in coltype:
        try:
            width = int(coltype.split("(", 1)[1].rstrip(")").strip())
        except ValueError:
            width = None

    if width is not None and width != resolved.embedding.dim:
        findings.append(
            Finding(
                check="column_dimension",
                ok=False,
                fatal=True,
                detail=(
                    f"embeddings.embedding is {coltype} but EMBEDDING_DIM is "
                    f"{resolved.embedding.dim}. Every insert would be rejected. "
                    "Run the migration for the current model, then re-index."
                ),
            )
        )
    else:
        findings.append(Finding("column_dimension", True, f"embeddings.embedding is {coltype}."))

    if kind != resolved.embedding.storage:
        findings.append(
            Finding(
                check="column_type",
                ok=False,
                fatal=True,
                detail=(
                    f"embeddings.embedding is '{kind}' but EMBEDDING_STORAGE is "
                    f"'{resolved.embedding.storage}'. The HNSW operator class is "
                    "chosen from the setting, so the two must agree."
                ),
            )
        )
    else:
        findings.append(Finding("column_type", True, kind))

    # A mixed index is worse than an empty one: vectors from two models occupy
    # unrelated spaces, so neighbours are meaningless while still being returned.
    #
    # Qualified for the same reason as the column lookup above: unqualified, this
    # reads whichever `embeddings` the search_path reaches first, and the path
    # ends in `public` so that extension-owned types resolve. An empty or absent
    # table in our own schema would therefore be answered by another
    # deployment's, and the models it reports back would belong to that one.
    try:
        models = (
            (
                await db.execute(
                    text(
                        f"SELECT DISTINCT model FROM {qualified_table} "  # noqa: S608 - config identifier, not user input
                        "WHERE model IS NOT NULL LIMIT 5"
                    )
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 - table may be empty or absent
        models = []

    stale = [m for m in models if m and m != resolved.embedding.model]
    if stale:
        findings.append(
            Finding(
                check="index_consistency",
                ok=False,
                fatal=False,
                detail=(
                    f"The vector index still holds embeddings from {', '.join(stale)}. "
                    f"Vectors from different models are not comparable - run "
                    f"`cip reindex-embeddings` to regenerate them as "
                    f"{resolved.embedding.model}."
                ),
            )
        )
    elif models:
        findings.append(
            Finding("index_consistency", True, f"All vectors are {resolved.embedding.model}.")
        )

    return findings, width, kind


async def diagnose(
    db: AsyncSession | None = None,
    *,
    probe_provider: bool = True,
    settings: Settings | None = None,
) -> EmbeddingDiagnostics:
    """Run every applicable check and collect the results."""
    resolved = settings or get_settings()
    embedding = resolved.embedding

    report = EmbeddingDiagnostics(
        provider=embedding.provider,
        model=embedding.model,
        configured_dim=embedding.dim,
        storage=embedding.storage,
        native_dim=embedding.native_dim,
    )
    report.findings.extend(check_configuration(resolved))

    # Only probe when the static checks passed: a probe with no credentials just
    # produces a second, less specific version of the same failure.
    if probe_provider and not [f for f in report.findings if not f.ok and f.fatal]:
        provider_findings, reported, latency = await check_provider(resolved)
        report.findings.extend(provider_findings)
        report.reported_dim = reported
        report.latency_ms = latency

    if db is not None:
        db_findings, width, kind = await check_database(db, resolved)
        report.findings.extend(db_findings)
        report.column_dim = width
        report.column_type = kind

    return report


def log_diagnostics(report: EmbeddingDiagnostics) -> None:
    """Emit the startup summary. One line per finding, so it greps."""
    logger.info(
        "embedding_configuration",
        provider=report.provider,
        model=report.model,
        configured_dim=report.configured_dim,
        native_dim=report.native_dim,
        reported_dim=report.reported_dim,
        column_dim=report.column_dim,
        column_type=report.column_type,
        storage=report.storage,
        latency_ms=report.latency_ms,
        ok=report.ok,
    )
    for finding in report.findings:
        if finding.ok:
            logger.info("embedding_check_passed", check=finding.check, detail=finding.detail)
        elif finding.fatal:
            logger.error("embedding_check_failed", check=finding.check, detail=finding.detail)
        else:
            logger.warning("embedding_check_warning", check=finding.check, detail=finding.detail)


def assert_ready(report: EmbeddingDiagnostics) -> None:
    """Abort startup when a fatal problem is present.

    Raising here rather than degrading is the point: an embedding pipeline that is
    misconfigured does not fail loudly at runtime, it returns worse answers. A
    process that will not start is the only signal that cannot be ignored.
    """
    fatal = report.fatal_problems
    if not fatal:
        return
    raise RuntimeError(
        "The embedding configuration is not usable:\n  - "
        + "\n  - ".join(f"{f.check}: {f.detail}" for f in fatal)
    )


__all__ = [
    "HNSW_MAX_DIMS",
    "EmbeddingDiagnostics",
    "Finding",
    "assert_ready",
    "check_configuration",
    "check_database",
    "check_provider",
    "diagnose",
    "log_diagnostics",
]

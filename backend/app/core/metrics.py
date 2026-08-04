"""Prometheus metrics.

One module owns every metric definition so names and label sets cannot drift
between the API, the worker pools and the Grafana dashboards in
``infra/grafana/dashboards``.

Label discipline: labels are bounded, low-cardinality dimensions only (stage,
queue, provider, model, strategy, outcome). **Never** label by ``project_id``,
``contract_id``, ``user_id`` or a raw path - that is what traces and logs are
for, and it would explode the time series count.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import CollectorRegistry as _Registry

from app.core.config import get_settings

# Latency buckets tuned for this workload: HTTP in milliseconds, pipeline stages
# in seconds-to-minutes (a 150-page parse legitimately takes minutes).
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_STAGE_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, 3600)
_AI_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)

REGISTRY: CollectorRegistry = CollectorRegistry(auto_describe=True)


# =============================================================================
# HTTP
# =============================================================================
http_requests_total = Counter(
    "cip_http_requests_total",
    "HTTP requests handled.",
    ["method", "route", "status_class"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "cip_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=_HTTP_BUCKETS,
    registry=REGISTRY,
)

http_requests_in_flight = Gauge(
    "cip_http_requests_in_flight",
    "HTTP requests currently being served.",
    registry=REGISTRY,
)

auth_attempts_total = Counter(
    "cip_auth_attempts_total",
    "Authentication attempts.",
    ["method", "outcome"],  # method: password|microsoft|refresh
    registry=REGISTRY,
)

authorization_denied_total = Counter(
    "cip_authorization_denied_total",
    "Authorization failures, by reason.",
    ["reason"],  # project_access|permission|inactive_user
    registry=REGISTRY,
)

rate_limit_rejections_total = Counter(
    "cip_rate_limit_rejections_total",
    "Requests rejected by the rate limiter.",
    ["scope"],
    registry=REGISTRY,
)


# =============================================================================
# Ingestion pipeline (§24 ingestion metrics)
# =============================================================================
uploads_total = Counter(
    "cip_uploads_total",
    "Documents accepted for processing.",
    ["file_type", "outcome"],  # outcome: accepted|duplicate|rejected
    registry=REGISTRY,
)

jobs_total = Gauge(
    "cip_jobs_total",
    "Processing jobs by state.",
    ["state"],
    registry=REGISTRY,
)

jobs_active = Gauge(
    "cip_jobs_active",
    "Processing jobs currently running a stage.",
    registry=REGISTRY,
)

jobs_created_total = Counter(
    "cip_jobs_created_total",
    "Processing jobs created.",
    ["priority"],
    registry=REGISTRY,
)

jobs_completed_total = Counter(
    "cip_jobs_completed_total",
    "Processing jobs that reached READY.",
    registry=REGISTRY,
)

jobs_failed_total = Counter(
    "cip_jobs_failed_total",
    "Processing jobs that reached FAILED.",
    ["stage"],
    registry=REGISTRY,
)

stage_started_total = Counter(
    "cip_stage_started_total",
    "Stage executions started.",
    ["stage"],
    registry=REGISTRY,
)

stage_completed_total = Counter(
    "cip_stage_completed_total",
    "Stage executions completed.",
    ["stage", "outcome"],  # outcome: succeeded|failed|skipped
    registry=REGISTRY,
)

stage_duration_seconds = Histogram(
    "cip_stage_duration_seconds",
    "Stage execution wall time.",
    ["stage"],
    buckets=_STAGE_BUCKETS,
    registry=REGISTRY,
)

stage_retry_total = Counter(
    "cip_stage_retry_total",
    "Stage retries.",
    ["stage"],
    registry=REGISTRY,
)

stage_checkpoint_reuse_total = Counter(
    "cip_stage_checkpoint_reuse_total",
    "Stages skipped because a version-compatible checkpoint already existed.",
    ["stage"],
    registry=REGISTRY,
)

worker_utilisation = Gauge(
    "cip_worker_utilisation",
    "Fraction of a pool's concurrency currently in use (0..1).",
    ["pool"],
    registry=REGISTRY,
)

processing_cost_usd_total = Counter(
    "cip_processing_cost_usd_total",
    "Estimated AI spend attributable to document processing.",
    ["stage", "model"],
    registry=REGISTRY,
)

pages_processed_total = Counter(
    "cip_pages_processed_total",
    "Document pages parsed.",
    ["parser"],
    registry=REGISTRY,
)

ocr_pages_total = Counter(
    "cip_ocr_pages_total",
    "Pages that required OCR.",
    ["engine"],
    registry=REGISTRY,
)


# =============================================================================
# Queue
# =============================================================================
queue_depth = Gauge(
    "cip_queue_depth",
    "Jobs waiting in a queue.",
    ["queue"],
    registry=REGISTRY,
)

queue_active = Gauge(
    "cip_queue_active",
    "Jobs actively being processed from a queue.",
    ["queue"],
    registry=REGISTRY,
)

queue_dlq_size = Gauge(
    "cip_queue_dlq_size",
    "Jobs in the dead letter queue.",
    registry=REGISTRY,
)

queue_enqueued_total = Counter(
    "cip_queue_enqueued_total",
    "Jobs enqueued.",
    ["queue", "priority"],
    registry=REGISTRY,
)

queue_enqueue_failures_total = Counter(
    "cip_queue_enqueue_failures_total",
    "Failed enqueue attempts.",
    ["queue"],
    registry=REGISTRY,
)


# =============================================================================
# Chunking / extraction
# =============================================================================
chunks_created_total = Counter(
    "cip_chunks_created_total",
    "Semantic chunks created.",
    ["strategy", "chunk_type"],
    registry=REGISTRY,
)

chunk_validation_failures_total = Counter(
    "cip_chunk_validation_failures_total",
    "Chunks rejected by the chunk validator.",
    ["reason"],
    registry=REGISTRY,
)

extractions_total = Counter(
    "cip_extractions_total",
    "Extraction attempts by category.",
    ["category", "outcome"],  # outcome: valid|schema_invalid|business_invalid|error
    registry=REGISTRY,
)

extracted_items_total = Counter(
    "cip_extracted_items_total",
    "Structured knowledge items persisted.",
    ["kind"],  # clause|entity|obligation|risk|timeline|relationship
    registry=REGISTRY,
)

review_triggered_total = Counter(
    "cip_review_triggered_total",
    "Items routed to human review.",
    ["trigger"],
    registry=REGISTRY,
)


# =============================================================================
# Embedding & indexing (§24 embedding metrics)
# =============================================================================
embedding_requested_total = Counter(
    "cip_embedding_requested_total",
    "Embedding vectors requested.",
    ["level"],
    registry=REGISTRY,
)

embedding_generated_total = Counter(
    "cip_embedding_generated_total",
    "Embedding vectors generated by a provider.",
    ["level", "provider"],
    registry=REGISTRY,
)

embedding_reused_total = Counter(
    "cip_embedding_reused_total",
    "Embedding vectors reused because content and versions were unchanged.",
    ["level"],
    registry=REGISTRY,
)

embedding_failures_total = Counter(
    "cip_embedding_failures_total",
    "Embedding generation failures.",
    ["provider"],
    registry=REGISTRY,
)

embedding_duration_seconds = Histogram(
    "cip_embedding_duration_seconds",
    "Embedding batch latency.",
    ["provider"],
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

vector_count = Gauge(
    "cip_vector_count",
    "Vectors stored, by level.",
    ["level"],
    registry=REGISTRY,
)

index_build_duration_seconds = Histogram(
    "cip_index_build_duration_seconds",
    "Search index build time.",
    buckets=_STAGE_BUCKETS,
    registry=REGISTRY,
)

graph_nodes_total = Gauge("cip_graph_nodes_total", "Knowledge graph nodes.", registry=REGISTRY)
graph_edges_total = Gauge("cip_graph_edges_total", "Knowledge graph edges.", registry=REGISTRY)


# =============================================================================
# Retrieval (§24 retrieval metrics)
# =============================================================================
retrieval_duration_seconds = Histogram(
    "cip_retrieval_duration_seconds",
    "End-to-end retrieval latency.",
    ["scope"],
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

retrieval_strategy_total = Counter(
    "cip_retrieval_strategy_total",
    "Retrieval strategy selections.",
    ["strategy", "intent"],
    registry=REGISTRY,
)

retrieval_candidates = Histogram(
    "cip_retrieval_candidates",
    "Candidate count returned per level before re-ranking.",
    ["level"],
    buckets=(0, 1, 5, 10, 25, 50, 100, 250, 500, 1000),
    registry=REGISTRY,
)

rerank_duration_seconds = Histogram(
    "cip_rerank_duration_seconds",
    "Cross-encoder re-ranking latency.",
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

# --- retrieval, split by leg -------------------------------------------------
#
# `retrieval_duration_seconds` is the total, which cannot answer the only
# question worth asking when retrieval is slow: *which leg*. A hybrid search runs
# a vector query, a keyword query and a fusion per level, and their costs scale
# with different things - HNSW with graph size and `ef_search`, `ts_rank` with
# corpus size and term frequency, fusion with candidate count alone. One number
# hides all three.
#
# Labelled by level as well as leg because the levels have very different
# cardinality: one summary per document against many chunks, so a chunk-level
# vector query is not comparable to a summary-level one.
retrieval_leg_duration_seconds = Histogram(
    "cip_retrieval_leg_duration_seconds",
    "Retrieval latency for one leg of one level.",
    ["leg", "level"],  # leg: vector|keyword|fusion
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

# --- per-clause extraction ---------------------------------------------------
#
# `CategoryOutcome` already records tokens, latency and evidence per clause, but
# only into the stage artifact - so the numbers exist per document and cannot be
# aggregated across a corpus without reading every artifact back. These export
# the same values, changing nothing about how they are produced.
#
# This is the pipeline's dominant cost: one structured call per clause category
# per document. Sizing any change to it - batching, prompt reordering, caching -
# requires knowing the split between evidence tokens (which vary per document)
# and scaffolding (which repeats identically on every call).
clause_extraction_tokens = Histogram(
    "cip_clause_extraction_tokens",
    "Tokens for one clause-category extraction call.",
    ["kind"],  # input|output|cache_read|evidence|repeated
    buckets=(0, 100, 250, 500, 1000, 2000, 4000, 8000, 16000, 32000),
    registry=REGISTRY,
)

clause_extraction_duration_seconds = Histogram(
    "cip_clause_extraction_duration_seconds",
    "Latency of one clause-category extraction call.",
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

# =============================================================================
# Copilot and evaluation
# =============================================================================
copilot_queries_total = Counter(
    "cip_copilot_queries_total",
    "Copilot questions, by how they were resolved.",
    # outcome: answered | insufficient_context | generation_failed | refused
    ["outcome", "retrieval_mode"],
    registry=REGISTRY,
)

copilot_similarity = Histogram(
    "cip_copilot_answerable_similarity",
    "Best clause/chunk similarity per question - what the guardrail compares.",
    buckets=(0.0, 0.1, 0.2, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    registry=REGISTRY,
)

copilot_context_chunks = Histogram(
    "cip_copilot_context_chunks",
    "Passages that reached the prompt, after re-ranking and the context budget.",
    buckets=(0, 1, 2, 4, 6, 8, 12, 16, 20),
    registry=REGISTRY,
)

#: Written by a benchmark run rather than by serving traffic, so a dashboard can
#: chart quality beside latency. Gauges, not counters: each run replaces the last
#: rather than accumulating.
evaluation_metric = Gauge(
    "cip_evaluation_metric",
    "Latest benchmark value, by dataset and metric.",
    ["dataset", "metric"],
    registry=REGISTRY,
)

evaluation_runs_total = Counter(
    "cip_evaluation_runs_total",
    "Benchmark runs, by whether the regression gate passed.",
    ["dataset", "result"],
    registry=REGISTRY,
)

graph_traversal_depth = Histogram(
    "cip_graph_traversal_depth",
    "Depth reached during graph traversal.",
    buckets=(1, 2, 3, 4, 5, 6),
    registry=REGISTRY,
)

search_duration_seconds = Histogram(
    "cip_search_duration_seconds",
    "Search API latency.",
    ["mode"],
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

cache_operations_total = Counter(
    "cip_cache_operations_total",
    "Cache hits and misses.",
    ["cache", "outcome"],
    registry=REGISTRY,
)


# =============================================================================
# RAG (§24 RAG metrics)
# =============================================================================
rag_duration_seconds = Histogram(
    "cip_rag_duration_seconds",
    "RAG inference latency.",
    ["response_format"],
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

rag_requests_total = Counter(
    "cip_rag_requests_total",
    "RAG requests.",
    ["response_format", "outcome"],  # outcome: ok|regenerated|failed|insufficient_evidence
    registry=REGISTRY,
)

rag_regenerated_total = Counter(
    "cip_rag_regenerated_total",
    "Answers regenerated after failing response validation.",
    ["reason"],
    registry=REGISTRY,
)

rag_citation_coverage = Gauge(
    "cip_rag_citation_coverage",
    "Share of answer segments carrying at least one citation (last window).",
    registry=REGISTRY,
)

rag_confidence = Histogram(
    "cip_rag_confidence",
    "Composite answer confidence.",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
    registry=REGISTRY,
)

llm_requests_total = Counter(
    "cip_llm_requests_total",
    "LLM calls.",
    ["provider", "model", "outcome"],
    registry=REGISTRY,
)

llm_tokens_total = Counter(
    "cip_llm_tokens_total",
    "LLM tokens consumed.",
    ["model", "kind"],  # kind: input|output|cache_read|cache_write
    registry=REGISTRY,
)

llm_cost_usd_total = Counter(
    "cip_llm_cost_usd_total",
    "Estimated LLM spend.",
    ["model"],
    registry=REGISTRY,
)

llm_duration_seconds = Histogram(
    "cip_llm_duration_seconds",
    "LLM call latency.",
    ["provider", "model"],
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

# --- routing-aware AI observability ------------------------------------------
#
# Labelled by task and tier, not only by model. A model name answers "what was
# billed"; the tier answers "was this workload routed correctly", which is the
# question that matters after the routing refactor. A rise in `simple`-tier
# latency and a fall in `complex` is a misroute, and no model-only metric shows it.
#
# Histograms rather than gauges because the brief asks for p95/p99, which are
# quantiles over a distribution - a gauge of "last latency" cannot produce them.
# Query with:  histogram_quantile(0.95, sum by (le, llm_tier)
#                (rate(cip_llm_task_duration_seconds_bucket[5m])))
llm_task_duration_seconds = Histogram(
    "cip_llm_task_duration_seconds",
    "LLM call latency by routed task and tier.",
    ["task", "tier", "provider", "model"],
    buckets=_AI_BUCKETS,
    registry=REGISTRY,
)

llm_retries_total = Counter(
    "cip_llm_retries_total",
    "LLM call retries, by why the attempt was retried.",
    ["provider", "tier", "reason"],  # reason: timeout|rate_limit|transport|server
    registry=REGISTRY,
)

llm_timeouts_total = Counter(
    "cip_llm_timeouts_total",
    "LLM calls abandoned at the per-tier timeout.",
    ["provider", "tier", "model"],
    registry=REGISTRY,
)

llm_payload_bytes = Histogram(
    "cip_llm_payload_bytes",
    "Prompt and completion sizes, for spotting evidence-budget regressions.",
    ["direction", "tier"],  # direction: input|output
    buckets=(256, 1024, 4096, 16_384, 65_536, 262_144, 1_048_576),
    registry=REGISTRY,
)

chunk_rejections_total = Counter(
    "cip_chunk_rejections_total",
    "Chunks discarded during validation, by the rule that refused them. Labelled by "
    "rule rather than reason so a miscalibrated threshold is distinguishable from a "
    "genuinely unusable document.",
    ["rule", "chunk_type"],
    registry=REGISTRY,
)

classification_fallback_total = Counter(
    "cip_classification_fallback_total",
    "Documents processed with the default profile because classification did not "
    "decide, by reason. A rising llm_unavailable is an outage; a rising ambiguous "
    "means the profiles' hints need work - the two look identical without this label.",
    ["reason"],
    registry=REGISTRY,
)

classification_confidence = Histogram(
    "cip_classification_confidence",
    "Confidence of the selected profile, by how it was chosen.",
    ["method"],  # method: rules|llm|forced|upload_hint|fallback
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0),
    registry=REGISTRY,
)

provider_health = Gauge(
    "cip_provider_health",
    "1 when the provider's last health check passed, else 0.",
    ["provider", "kind"],  # kind: llm|embedding|parser|storage
    registry=REGISTRY,
)

hallucination_flags_total = Counter(
    "cip_hallucination_flags_total",
    "Responses flagged by grounding validation.",
    ["check"],
    registry=REGISTRY,
)


# =============================================================================
# Business gauges (refreshed by the scheduler)
# =============================================================================
contracts_total = Gauge(
    "cip_contracts_total",
    "Contracts by status.",
    ["status"],
    registry=REGISTRY,
)

contracts_expiring = Gauge(
    "cip_contracts_expiring",
    "Contracts expiring within the alert window.",
    registry=REGISTRY,
)

contracts_high_risk = Gauge(
    "cip_contracts_high_risk",
    "Contracts in the high risk band.",
    registry=REGISTRY,
)

alerts_open = Gauge(
    "cip_alerts_open",
    "Open alerts by type.",
    ["alert_type"],
    registry=REGISTRY,
)

exports_total = Counter(
    "cip_exports_total",
    "Export jobs.",
    ["format", "outcome"],
    registry=REGISTRY,
)


# =============================================================================
# Helpers
# =============================================================================
@contextmanager
def observe_duration(histogram: Histogram, **labels: str) -> Iterator[None]:
    """Time a block into ``histogram``. Records even when the block raises."""
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - started
        (histogram.labels(**labels) if labels else histogram).observe(elapsed)


@contextmanager
def track_in_progress(gauge: Gauge, **labels: str) -> Iterator[None]:
    target = gauge.labels(**labels) if labels else gauge
    target.inc()
    try:
        yield
    finally:
        target.dec()


def status_class(status_code: int) -> str:
    """``503`` -> ``5xx``. Keeps HTTP metric cardinality at five series."""
    return f"{status_code // 100}xx"


def render_metrics() -> tuple[bytes, str]:
    """Render the exposition payload. Returns ``(body, content_type)``.

    Under Gunicorn the ``PROMETHEUS_MULTIPROC_DIR`` collector is used so counters
    aggregate across worker processes instead of reporting one worker's view.
    """
    import os

    from prometheus_client import CONTENT_TYPE_LATEST

    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if multiproc_dir:
        registry: _Registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def metrics_enabled() -> bool:
    return get_settings().observability.metrics_enabled


def safe(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator: a metrics failure must never break the request it measures."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            return None

    return wrapper


__all__ = [
    "REGISTRY",
    "alerts_open",
    "auth_attempts_total",
    "authorization_denied_total",
    "cache_operations_total",
    "chunk_rejections_total",
    "chunk_validation_failures_total",
    "chunks_created_total",
    "classification_confidence",
    "classification_fallback_total",
    "contracts_expiring",
    "contracts_high_risk",
    "contracts_total",
    "copilot_context_chunks",
    "copilot_queries_total",
    "copilot_similarity",
    "embedding_duration_seconds",
    "embedding_failures_total",
    "embedding_generated_total",
    "embedding_requested_total",
    "embedding_reused_total",
    "evaluation_metric",
    "evaluation_runs_total",
    "exports_total",
    "extracted_items_total",
    "extractions_total",
    "graph_edges_total",
    "graph_nodes_total",
    "graph_traversal_depth",
    "hallucination_flags_total",
    "http_request_duration_seconds",
    "http_requests_in_flight",
    "http_requests_total",
    "index_build_duration_seconds",
    "jobs_active",
    "jobs_completed_total",
    "jobs_created_total",
    "jobs_failed_total",
    "jobs_total",
    "llm_cost_usd_total",
    "llm_duration_seconds",
    "llm_payload_bytes",
    "llm_requests_total",
    "llm_retries_total",
    "llm_task_duration_seconds",
    "llm_timeouts_total",
    "llm_tokens_total",
    "metrics_enabled",
    "observe_duration",
    "ocr_pages_total",
    "pages_processed_total",
    "processing_cost_usd_total",
    "provider_health",
    "queue_active",
    "queue_depth",
    "queue_dlq_size",
    "queue_enqueue_failures_total",
    "queue_enqueued_total",
    "rag_citation_coverage",
    "rag_confidence",
    "rag_duration_seconds",
    "rag_regenerated_total",
    "rag_requests_total",
    "rate_limit_rejections_total",
    "render_metrics",
    "rerank_duration_seconds",
    "retrieval_candidates",
    "retrieval_duration_seconds",
    "retrieval_strategy_total",
    "review_triggered_total",
    "safe",
    "search_duration_seconds",
    "stage_checkpoint_reuse_total",
    "stage_completed_total",
    "stage_duration_seconds",
    "stage_retry_total",
    "stage_started_total",
    "status_class",
    "track_in_progress",
    "uploads_total",
    "vector_count",
    "worker_utilisation",
]

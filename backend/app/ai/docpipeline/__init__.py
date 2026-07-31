"""Document pipeline: page JSON in, classified and clause-tagged rows out.

A standalone sequence that starts from the per-page JSON already on disk. It
does not call the PDF service, does not use the stage orchestrator, and does not
need Redis, MinIO or the queue workers - which is the point: it can be run and
re-run against a directory of ``page_*.json`` while the shape of the pipeline is
still being decided.

    from pathlib import Path
    from app.ai.docpipeline import run_document_pipeline

    outcome = await run_document_pipeline(Path("…/json_output"))
"""

from app.ai.docpipeline.classification import (
    CLASSIFICATION_PAGE_WINDOW,
    DocumentClassification,
    DocumentTypeClassifier,
)
from app.ai.docpipeline.clauses import (
    DEFAULT_CHUNK_PAGES,
    MIN_CHUNK_CHARS,
    ClauseDetection,
    ClauseDetector,
    DetectedClause,
)
from app.ai.docpipeline.mapping import ClauseSpec, load_clauses, load_doc_types
from app.ai.docpipeline.persistence import persist_document
from app.ai.docpipeline.runner import PipelineOutcome, run_document_pipeline, summarise
from app.ai.docpipeline.source import PageContent, Paragraph, load_pages
from app.ai.docpipeline.vectors import embed_texts

__all__ = [
    "CLASSIFICATION_PAGE_WINDOW",
    "DEFAULT_CHUNK_PAGES",
    "MIN_CHUNK_CHARS",
    "ClauseDetection",
    "ClauseDetector",
    "ClauseSpec",
    "DetectedClause",
    "DocumentClassification",
    "DocumentTypeClassifier",
    "PageContent",
    "Paragraph",
    "PipelineOutcome",
    "embed_texts",
    "load_clauses",
    "load_doc_types",
    "load_pages",
    "persist_document",
    "run_document_pipeline",
    "summarise",
]

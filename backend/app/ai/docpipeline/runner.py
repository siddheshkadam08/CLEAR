"""The pipeline sequence, and the report it prints.

Stages run in order and each one's output is the next one's input:

    load pages -> classify -> look up clauses -> detect -> embed -> persist

Every stage after the first can be turned off from the CLI, so the cheap parts
can be exercised without spending on the expensive ones.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.ai.docpipeline.classification import (
    CLASSIFICATION_PAGE_WINDOW,
    DocumentClassification,
    DocumentTypeClassifier,
)
from app.ai.docpipeline.clauses import (
    DEFAULT_CHUNK_CONCURRENCY,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_PAGES,
    ClauseDetection,
    ClauseDetector,
)
from app.ai.docpipeline.mapping import ClauseSpec, load_clauses, load_doc_types
from app.ai.docpipeline.persistence import PersistedDocument, persist_document
from app.ai.docpipeline.source import PageContent, load_pages
from app.ai.docpipeline.vectors import EmbeddingOutcome, embed_texts
from app.core.logging import get_logger
from app.db.session import session_scope

logger = get_logger(__name__)

Emit = Callable[[str], None]


@dataclass(slots=True)
class PipelineOutcome:
    source_dir: Path
    pages: list[PageContent] = field(default_factory=list)
    doc_types: list[str] = field(default_factory=list)
    classification: DocumentClassification | None = None
    clause_specs: list[ClauseSpec] = field(default_factory=list)
    detection: ClauseDetection | None = None
    embeddings: EmbeddingOutcome | None = None
    persisted: PersistedDocument | None = None

    @property
    def paragraph_count(self) -> int:
        return sum(len(page.paragraphs) for page in self.pages)

    @property
    def heading_count(self) -> int:
        return sum(1 for page in self.pages for para in page.paragraphs if para.is_heading)


async def run_document_pipeline(
    source_dir: Path,
    *,
    pdf_path: str | None = None,
    page_window: int = CLASSIFICATION_PAGE_WINDOW,
    chunk_pages: int = DEFAULT_CHUNK_PAGES,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    concurrency: int = DEFAULT_CHUNK_CONCURRENCY,
    early_stop: bool = True,
    classify: bool = True,
    persist: bool = True,
    verbose: bool = False,
    emit: Emit = print,
) -> PipelineOutcome:
    outcome = PipelineOutcome(source_dir=source_dir)

    # ------------------------------------------------------------- stage 0
    outcome.pages = load_pages(source_dir)
    emit("CLEAR document pipeline")
    emit(f"source : {source_dir}")
    emit(
        f"loaded : {len(outcome.pages)} pages, {outcome.paragraph_count} paragraphs "
        f"({outcome.heading_count} section headings)"
    )

    if verbose:
        _report_all_pages(outcome, emit)

    if not classify:
        _report_window(outcome, page_window, emit)
        emit("")
        emit("Stopped after extraction (--dry-run).")
        return outcome

    async with session_scope() as db:
        outcome.doc_types = await load_doc_types(db)

        # --------------------------------------------------------- stage 1
        _report_window(outcome, page_window, emit)
        classification = await DocumentTypeClassifier().classify(
            outcome.pages, outcome.doc_types, page_window=page_window
        )
        outcome.classification = classification
        _report_classification(classification, emit)

        outcome.clause_specs = await load_clauses(db, classification.doc_type)
        emit("")
        emit(
            f"===== CLAUSE TARGETS (cip_docMapping / {classification.doc_type}) ====="
            f"  {len(outcome.clause_specs)} clauses"
        )
        if not outcome.clause_specs:
            emit("  none - nothing to detect.")
            return outcome

        # --------------------------------------------------------- stage 2
        detection = await ClauseDetector().detect(
            outcome.pages,
            outcome.clause_specs,
            chunk_pages=chunk_pages,
            chunk_overlap=chunk_overlap,
            concurrency=concurrency,
            early_stop=early_stop,
        )
        outcome.detection = detection
        _report_detection(outcome, detection, page_window, chunk_pages, chunk_overlap, emit)
        if verbose:
            _report_clause_text(detection, emit)

        if not persist:
            emit("")
            emit("Stopped before embedding and persistence (--no-persist).")
            return outcome

        # --------------------------------------------------------- stage 3
        emit("")
        emit("===== EMBEDDINGS =====")
        if not detection.detected:
            emit("  no clauses detected - nothing to embed.")
            return outcome

        embeddings = await embed_texts([item.textcontent for item in detection.detected])
        outcome.embeddings = embeddings
        emit(
            f"  {len(embeddings.vectors)} texts in {embeddings.batches} batch(es) | "
            f"{embeddings.model} | dim {embeddings.dim} | {embeddings.duration_ms} ms"
        )

        # --------------------------------------------------------- stage 4
        persisted = await persist_document(
            db,
            doc_type=classification.doc_type,
            json_dir=source_dir,
            pdf_path=pdf_path,
            clauses=detection.detected,
            vectors=embeddings.vectors,
        )
        outcome.persisted = persisted
        emit("")
        emit("===== PERSISTED =====")
        if persisted.replaced_previous:
            emit("  replaced a previous run over the same source directory")
        emit(f"  cip_DocMaster         docid={persisted.docid}")
        emit(f"  cip_DocContentMaster  {persisted.clause_rows} rows")

    return outcome


# ----------------------------------------------------------------- report
def _report_window(outcome: PipelineOutcome, page_window: int, emit: Emit) -> None:
    window = outcome.pages[:page_window]
    if not window:
        return
    emit("")
    emit(
        f"===== CLASSIFICATION INPUT  (pages {window[0].page_number}-"
        f"{window[-1].page_number} of {len(outcome.pages)}) ====="
    )
    for page in window:
        emit("")
        emit(f"--- PAGE {page.page_number}  ({len(page.paragraphs)} paragraphs) ---")
        for paragraph in page.paragraphs:
            role = f" ({paragraph.role})" if paragraph.role else ""
            emit(f"  [{paragraph.ref}]{role} {paragraph.content}")


def _report_classification(classification: DocumentClassification, emit: Emit) -> None:
    emit("")
    emit("===== DOCUMENT CLASSIFICATION =====")
    emit(f"  document type : {classification.doc_type}")
    emit(f"  confidence    : {classification.confidence:.2f}")
    emit(f"  reason        : {classification.reason}")
    emit(
        f"  model         : {classification.model} | "
        f"{classification.pages_used} pages | {classification.duration_ms} ms"
    )
    if classification.fell_back:
        emit("  note          : the model's answer was off-taxonomy; fell back to this type.")


def _report_detection(
    outcome: PipelineOutcome,
    detection: ClauseDetection,
    page_window: int,
    chunk_pages: int,
    chunk_overlap: int,
    emit: Emit,
) -> None:
    emit("")
    emit("--- heading pass ---")
    heading_found = [item for item in detection.detected if item.method.startswith("heading")]
    for item in heading_found:
        tag = "exact" if item.method == "heading:exact" else "llm  "
        emit(f"  [{tag}] {item.clause:<34} {item.page_span:<9} {len(item.paragraphs)} paras")
    emit(
        f"  {len(heading_found)} found, {detection.total_targets - len(heading_found)} outstanding"
    )

    if detection.chunk_outcomes:
        emit("")
        emit(
            f"--- chunk fallback ({chunk_pages} pages per call, {chunk_overlap}-page "
            "overlap, outstanding carried forward) ---"
        )
        for chunk in detection.chunk_outcomes:
            label = f"  pages {chunk.start_page:>2}-{chunk.end_page:<2}"
            if chunk.skipped:
                emit(f"{label} skipped ({chunk.skip_reason})")
            else:
                emit(f"{label} -> {len(chunk.found)} found, {chunk.outstanding_after} outstanding")
        if detection.early_stopped and detection.pages_never_read:
            emit("  every clause found - remaining chunks skipped")
        else:
            emit("  chunks exhausted - whole document read")

    emit("")
    emit("===== CLAUSE DETECTION SUMMARY =====")
    doc_type = outcome.classification.doc_type if outcome.classification else "?"
    emit(f"  document type : {doc_type}   (cip_docMapping: {detection.total_targets} clauses)")
    percent = (
        (len(detection.detected) / detection.total_targets * 100) if detection.total_targets else 0
    )
    emit(
        f"  detected      : {len(detection.detected)} / {detection.total_targets}   ({percent:.0f}%)"
    )

    emit("")
    emit(f"  FOUND ({len(detection.detected)})")
    for item in sorted(detection.detected, key=lambda c: (c.page_numbers or [0])[0]):
        repaired = "  (boundary-repaired)" if item.boundary_repaired else ""
        emit(
            f"    {item.clause:<34} {item.method:<14} {item.page_span:<9} "
            f"{len(item.paragraphs)} paras{repaired}"
        )

    emit("")
    emit(f"  NOT FOUND ({len(detection.not_found)})")
    # This wording is load-bearing. NOT FOUND is only ever reached after the
    # whole document was read - the detector raises if that stops being true -
    # so it is safe to state it as a fact about the document.
    scope = f"searched all {len(outcome.pages)} pages - absent"
    for clause in detection.not_found:
        emit(f"    {clause:<34} {scope}")
    if not detection.not_found:
        emit("    (none)")

    emit("")
    emit("===== PAGES READ / NOT READ =====")
    window = outcome.pages[:page_window]
    if window:
        emit(
            f"  classification : {window[0].page_number}-{window[-1].page_number}"
            f"     (page window = {page_window})"
        )
    emit(
        f"  heading pass   : all {len(outcome.pages)} pages    (headings only - no body text sent)"
    )

    sent = detection.pages_sent_to_chunks
    if sent:
        emit(f"  chunk fallback : {sent[0]}-{sent[-1]}    ({detection.llm_chunk_calls} call(s))")
    else:
        emit("  chunk fallback : none    (every clause matched a heading)")

    if detection.pages_never_read:
        never = detection.pages_never_read
        emit(f"  NEVER READ     : {never[0]}-{never[-1]}    ({len(never)} pages)")
        emit("                   why: every clause was found before these pages were")
        emit("                   reached, so nothing remained to search for.")
        emit("                   No clause is reported NOT FOUND.")
    else:
        emit("  NEVER READ     : none")
        if detection.not_found:
            emit("                   why: clauses were still outstanding, so every")
            emit("                   chunk ran to the end of the document.")

    skipped = [chunk for chunk in detection.chunk_outcomes if chunk.skipped]
    if skipped:
        emit("")
        emit("  pages skipped without a call (reached and evaluated, not unread):")
        for chunk in skipped:
            emit(f"    {chunk.start_page}-{chunk.end_page}   {chunk.skip_reason}")


def _report_clause_text(detection: ClauseDetection, emit: Emit) -> None:
    """Print the full text, pages and box of every clause that will be stored.

    This is what actually goes into ``cip_DocContentMaster.textcontent`` and gets
    embedded, so seeing it is the only way to catch a clause that was located
    correctly but whose extent is wrong.
    """
    emit("")
    emit("===== CLAUSE TEXT (what gets stored and embedded) =====")
    for item in sorted(detection.detected, key=lambda c: (c.page_numbers or [0])[0]):
        emit("")
        emit(f"--- {item.clause}  [{item.method}]  pages {item.page_numbers} ---")
        polygon = item.polygon
        if polygon:
            box = ", ".join(f"{value:.3f}" for value in polygon)
            emit(f"    polygon (inches): [{box}]")
        emit(f"    refs   : {', '.join(p.ref for p in item.paragraphs)}")
        emit(f"    chars  : {len(item.textcontent)}")
        for paragraph in item.paragraphs:
            emit(f"    [{paragraph.ref}] {paragraph.content}")


def _report_all_pages(outcome: PipelineOutcome, emit: Emit) -> None:
    """Every paragraph of every page, in sequence."""
    emit("")
    emit(f"===== FULL DOCUMENT TEXT ({len(outcome.pages)} pages) =====")
    for page in outcome.pages:
        emit("")
        emit(f"--- PAGE {page.page_number}  ({len(page.paragraphs)} paragraphs) ---")
        for paragraph in page.paragraphs:
            role = f" ({paragraph.role})" if paragraph.role else ""
            emit(f"  [{paragraph.ref}]{role} {paragraph.content}")


def summarise(outcome: PipelineOutcome) -> dict[str, object]:
    """Machine-readable summary, for logs and tests."""
    detection = outcome.detection
    return {
        "source_dir": str(outcome.source_dir),
        "pages": len(outcome.pages),
        "paragraphs": outcome.paragraph_count,
        "doc_type": outcome.classification.doc_type if outcome.classification else None,
        "clause_targets": detection.total_targets if detection else 0,
        "clauses_found": len(detection.detected) if detection else 0,
        "clauses_not_found": list(detection.not_found) if detection else [],
        "pages_never_read": list(detection.pages_never_read) if detection else [],
        "early_stopped": detection.early_stopped if detection else False,
        "chunk_calls": detection.llm_chunk_calls if detection else 0,
        "docid": outcome.persisted.docid if outcome.persisted else None,
    }

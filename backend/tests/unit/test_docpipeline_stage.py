"""The document pipeline as a pipeline stage, and the order it runs in."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

from app.core.enums import (
    STAGE_ARTIFACTS,
    STAGE_DEPENDENCIES,
    STAGE_ORDER,
    STAGE_TO_STATE,
    ArtifactKind,
    JobState,
    PipelineStage,
)


def test_the_pipeline_is_validation_parser_docpipeline_extraction_embedding_indexing() -> None:
    """Six stages, not eight.

    DOCPIPELINE locates clauses; EXTRACTION turns them into typed knowledge and
    writes the `chunks` rows the keyword search reads; EMBEDDING vectorises those
    so semantic search and Copilot have something to retrieve; INDEXING derives
    the graph edges retrieval traverses. The remaining original stages stay as
    enum members because historical job_stage_runs rows name them.
    """
    assert STAGE_ORDER == (
        PipelineStage.VALIDATION,
        PipelineStage.PARSER,
        PipelineStage.DOCPIPELINE,
        PipelineStage.EXTRACTION,
        PipelineStage.EMBEDDING,
        PipelineStage.INDEXING,
    )


def test_the_old_stages_are_no_longer_dispatched() -> None:
    """INDEXING is deliberately *not* in this set.

    It was retired alongside the others, but unlike them it had no replacement:
    the derived edges it writes into `knowledge_relationships` are what
    `RetrievalEngine._expand_graph` traverses, and nothing else produces them.
    Dropping it degraded retrieval silently rather than visibly, which is why it
    is back in STAGE_ORDER while these four stay out.
    """
    retired = {
        PipelineStage.ENRICHMENT,
        PipelineStage.CLASSIFICATION,
        PipelineStage.CHUNKING,
        PipelineStage.AI_EXTRACTION,
    }

    assert not retired & set(STAGE_ORDER)
    # ...but still resolvable, so a historical run renders rather than raising.
    for stage in retired:
        assert stage in STAGE_TO_STATE
        assert stage in STAGE_DEPENDENCIES


def test_indexing_runs_last_and_after_embedding() -> None:
    """Its `requires` and its dependency both point at earlier stages.

    Ordering matters more than usual here: the stage refuses to run when the
    contract has no vectors, so scheduling it before EMBEDDING would fail every
    job rather than merely produce a thin graph.
    """
    assert STAGE_ORDER[-1] is PipelineStage.INDEXING
    assert STAGE_DEPENDENCIES[PipelineStage.INDEXING] == (PipelineStage.EMBEDDING,)


def test_indexing_does_not_claim_an_artifact_kind_extraction_owns() -> None:
    """Two stages emitting one kind makes re-running either invalidate the other.

    `DocumentArtifactRepository.invalidate_from_stage` supersedes by kind, so
    when indexing also emitted RELATIONSHIPS - which EXTRACTION declares - a
    reprocess of one silently dropped the other's output.
    """
    assert ArtifactKind.RELATIONSHIPS in STAGE_ARTIFACTS[PipelineStage.EXTRACTION]
    assert ArtifactKind.RELATIONSHIPS not in STAGE_ARTIFACTS[PipelineStage.INDEXING]


def test_docpipeline_depends_on_the_parser() -> None:
    """It reads the parser's cached page JSON, so the parse must have happened."""
    assert STAGE_DEPENDENCIES[PipelineStage.DOCPIPELINE] == (PipelineStage.PARSER,)


def test_docpipeline_has_a_job_state_and_an_artifact() -> None:
    assert STAGE_TO_STATE[PipelineStage.DOCPIPELINE] is JobState.AI_EXTRACTION
    assert STAGE_ARTIFACTS[PipelineStage.DOCPIPELINE] == (ArtifactKind.DOC_PIPELINE,)


def test_the_handler_is_registered_and_wired() -> None:
    from app.orchestrator.stages.base import get_stage_handler

    handler = get_stage_handler(PipelineStage.DOCPIPELINE)

    assert handler.stage is PipelineStage.DOCPIPELINE
    assert ArtifactKind.NORMALIZED_DOCUMENT in handler.requires
    assert handler.retryable


def test_the_ai_worker_serves_the_new_stage() -> None:
    """Otherwise the queue dispatches to a role that will not pick it up."""
    from app.api.internal import stages_for_role

    assert PipelineStage.DOCPIPELINE in stages_for_role("ai")
    assert PipelineStage.DOCPIPELINE in stages_for_role("all")


def test_every_document_type_has_a_profile_to_resolve_to() -> None:
    """Each `cip_docMapping` docType must reach a seeded Document Profile.

    The profile decides the mandatory-clause list a document is scored against,
    so a type that falls through to the default is judged by another type's
    clauses. That is precisely the failure that retired the old rules classifier
    - a License Agreement checked against an NDA's four mandatory clauses found
    none of them and failed the job - and it came back in a quieter form when
    only MSA and NDA had profiles and the other four defaulted silently.

    `other` is the deliberate exception: it means "unrecognised", and the default
    profile is the honest answer for a document nobody could type.
    """
    from app.ai.docpipeline.taxonomy import agreement_type_for
    from app.db.seed import PROFILE_SEEDS

    seeded = {spec["agreement_type"] for spec in PROFILE_SEEDS}

    for doc_type in ("MSA", "NDA", "License Agreement", "Contract cum Order Form", "Addendum"):
        agreement_type, _ = agreement_type_for(doc_type)
        value = getattr(agreement_type, "value", agreement_type)
        assert value in seeded, f"{doc_type} -> {value} has no profile"

    assert any(spec.get("is_default") for spec in PROFILE_SEEDS), (
        "Others/unrecognised documents need a default profile to fall back to."
    )


def test_progress_reaches_100_at_the_end_of_the_pipeline() -> None:
    """The bar must finish, and must not go backwards between stages."""
    from app.orchestrator.workflow import WorkflowEngine

    assert WorkflowEngine.progress_for(PipelineStage.VALIDATION) == 0
    assert WorkflowEngine.progress_for(STAGE_ORDER[-1], completed=True) == 100
    # Each stage starts where the previous one finished, so the bar never jumps
    # backwards at a boundary.
    for earlier, later in pairwise(STAGE_ORDER):
        assert WorkflowEngine.progress_for(earlier, completed=True) == WorkflowEngine.progress_for(
            later
        ), f"gap between {earlier} and {later}"


def test_versions_cover_what_changes_the_output() -> None:
    from app.core.versions import current_versions_for_stage

    versions = current_versions_for_stage(PipelineStage.DOCPIPELINE)

    # The parse it reads, the model that reads it, and the embedder.
    assert versions.parser_name
    assert versions.extraction_engine_version
    assert versions.model_name
    assert versions.embedding_model


# ------------------------------------------------------------ payload loading
def test_pages_can_be_built_from_payloads_without_touching_disk() -> None:
    """The stage gets the parser's cached payloads in memory, not a directory."""
    from app.ai.docpipeline.source import pages_from_payloads

    payloads = [
        {
            "pages": [{"pageNumber": 1}],
            "paragraphs": [
                {"content": "1. DEFINITIONS", "role": "sectionHeading"},
                {"content": "1.1 In this Agreement ..."},
            ],
        },
        {
            "pages": [{"pageNumber": 2}],
            "paragraphs": [{"content": "2.1 The Licensor grants ..."}],
        },
    ]

    pages = pages_from_payloads(payloads)

    assert [p.page_number for p in pages] == [1, 2]
    assert pages[0].paragraphs[0].is_heading
    assert pages[0].paragraphs[1].ref == "1.2"
    assert pages[1].paragraphs[0].content.startswith("2.1")


def test_payload_pages_fall_back_to_position(tmp_path: Path) -> None:
    """A payload with no page number still belongs where the cache put it."""
    from app.ai.docpipeline.source import pages_from_payloads

    pages = pages_from_payloads(
        [{"pages": [], "paragraphs": [{"content": "orphan"}]}],
        source_dir=tmp_path,
    )

    assert pages[0].page_number == 1
    assert pages[0].source_file.name == "page_1.json"


# ------------------------------------------- plans built before the switch
def test_stage_position_tolerates_a_retired_stage() -> None:
    """An execution_plan is persisted, so it outlives the STAGE_ORDER it was
    built from."""
    from app.core.enums import stage_index, stage_position

    assert stage_position(PipelineStage.DOCPIPELINE) == 2
    assert stage_position(PipelineStage.AI_EXTRACTION) is None
    # The strict form still raises, for callers that genuinely require an index.
    try:
        stage_index(PipelineStage.AI_EXTRACTION)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("stage_index should raise for a retired stage")


def test_next_after_skips_retired_stages_in_an_old_plan() -> None:
    """The regression that dead-lettered a job.

    A job planned before the switch still lists enrichment..indexing. Walking
    that plan called STAGE_ORDER.index on a stage no longer in it, raising
    ValueError inside the stage-completion path - which surfaced as a 500, which
    the queue could only interpret as a transport failure. Three retries later
    the job was dead-lettered, having looked to the operator like nothing
    happened at all.
    """
    import uuid

    from app.orchestrator.workflow import ExecutionPlan, PlannedStage, WorkflowEngine

    plan = ExecutionPlan(
        job_id=uuid.uuid4(),
        contract_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        stages=[
            PlannedStage(stage=PipelineStage.VALIDATION),
            PlannedStage(stage=PipelineStage.PARSER),
            PlannedStage(stage=PipelineStage.ENRICHMENT),
            PlannedStage(stage=PipelineStage.AI_EXTRACTION),
            PlannedStage(stage=PipelineStage.DOCPIPELINE),
        ],
    )

    # `next_after` reads the plan and nothing else, so the session is unused.
    engine = WorkflowEngine(db=None)  # type: ignore[arg-type]

    # Does not raise, and picks the one stage that is still dispatchable.
    assert engine.next_after(plan, PipelineStage.PARSER) is PipelineStage.DOCPIPELINE
    assert engine.next_after(plan, PipelineStage.DOCPIPELINE) is None


# ------------------------------------------------------- queue deduplication
def test_each_dispatch_gets_its_own_id() -> None:
    """A deliberate re-run must not collapse onto the run that already finished.

    The queue keys a BullMQ job on (job, stage, attempt) so a duplicate HTTP
    delivery of one enqueue collapses instead of running the stage twice.
    Completed jobs are retained, so that key also matched a reprocess or a
    Retry, and BullMQ dropped it - `stage_enqueued` with no `stage_dispatched`
    and nothing anywhere saying the request had been ignored.
    """
    import uuid as _uuid

    from app.orchestrator.queue import StageMessage

    common = {
        "job_id": _uuid.uuid4(),
        "contract_id": _uuid.uuid4(),
        "project_id": _uuid.uuid4(),
        "stage": PipelineStage.DOCPIPELINE,
    }

    first = StageMessage(**common)
    second = StageMessage(**common)

    assert first.dispatch_id != second.dispatch_id
    assert first.dispatch_id in first.to_payload()["dispatch_id"]


def test_a_redelivered_message_keeps_its_dispatch_id() -> None:
    """The other half: re-sending the same payload must still collapse."""
    import uuid as _uuid

    from app.orchestrator.queue import StageMessage

    original = StageMessage(
        job_id=_uuid.uuid4(),
        contract_id=_uuid.uuid4(),
        project_id=_uuid.uuid4(),
        stage=PipelineStage.DOCPIPELINE,
    )

    redelivered = StageMessage.from_payload(original.to_payload())

    assert redelivered.dispatch_id == original.dispatch_id

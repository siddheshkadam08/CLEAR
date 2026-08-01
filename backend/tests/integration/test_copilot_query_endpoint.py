"""The ``/copilot/query`` contract, and the rule that keeps retrieval off ``cip_*``.

Two tiers, matching the convention in ``test_embedding_reuse_lookup``:

* **Contract-level** tests run everywhere. They pin the route, the camelCase wire
  format, and - the one worth having most - that the query path cannot reach
  ``cip_DocContentMaster``. That table is written by the ingest pipeline and read
  by nothing at answer time; a well-meaning "just join the clause text" would be
  easy to add, hard to notice, and would put a second, unversioned source of
  clause text behind answers that are supposed to come from the vector store.
* **Live** tests need ``TEST_DATABASE_URL`` and exercise the service against a
  real session.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Every module the Copilot touches between receiving a question and returning an
#: answer. If one of these starts importing the ingest tables, the isolation test
#: below fails.
QUERY_PATH_MODULES = (
    "services/copilot.py",
    "ai/retrieval/engine.py",
    "ai/retrieval/planner.py",
    "ai/retrieval/context.py",
    "ai/retrieval/rerank.py",
    "ai/retrieval/analysis.py",
    "ai/rag/engine.py",
    "repositories/embedding.py",
)


class TestRoute:
    def test_the_route_is_registered(self) -> None:
        from app.api.v1.search import copilot_router

        paths = {route.path for route in copilot_router.routes}  # type: ignore[attr-defined]
        assert "/copilot/query" in paths

    def test_it_is_a_post(self) -> None:
        from app.api.v1.search import copilot_router

        route = next(r for r in copilot_router.routes if r.path == "/copilot/query")  # type: ignore[attr-defined]
        assert route.methods == {"POST"}  # type: ignore[attr-defined]

    def test_it_declares_the_camel_case_response(self) -> None:
        from app.api.v1.search import copilot_router
        from app.schemas.copilot import CopilotQueryResponse

        route = next(r for r in copilot_router.routes if r.path == "/copilot/query")  # type: ignore[attr-defined]
        assert route.response_model is CopilotQueryResponse  # type: ignore[attr-defined]

    def test_the_existing_copilot_routes_are_untouched(self) -> None:
        """`/ask` and `/stream` have callers; adding `/query` must not disturb them."""
        from app.api.v1.search import copilot_router

        paths = {route.path for route in copilot_router.routes}  # type: ignore[attr-defined]
        assert {"/copilot/ask", "/copilot/stream"} <= paths


class TestWireFormat:
    def test_a_camel_case_body_is_accepted(self) -> None:
        from app.schemas.copilot import CopilotQueryRequest

        request = CopilotQueryRequest.model_validate(
            {
                "projectId": "11111111-1111-1111-1111-111111111111",
                "contractId": "22222222-2222-2222-2222-222222222222",
                "query": "What is the termination notice period?",
            }
        )

        assert str(request.project_id) == "11111111-1111-1111-1111-111111111111"
        assert str(request.contract_id) == "22222222-2222-2222-2222-222222222222"

    def test_the_python_side_names_still_construct(self) -> None:
        """`populate_by_name` is what lets services build these without aliases."""
        from app.schemas.copilot import CopilotQueryRequest

        assert CopilotQueryRequest(query="q").query == "q"

    def test_an_empty_query_is_rejected(self) -> None:
        from pydantic import ValidationError

        from app.schemas.copilot import CopilotQueryRequest

        with pytest.raises(ValidationError):
            CopilotQueryRequest.model_validate({"query": "   "})

    def test_an_unknown_field_is_rejected_rather_than_ignored(self) -> None:
        """A typo'd `contractID` must fail loudly, not silently widen the search."""
        from pydantic import ValidationError

        from app.schemas.copilot import CopilotQueryRequest

        with pytest.raises(ValidationError):
            CopilotQueryRequest.model_validate({"query": "q", "contractID": "x"})

    def test_the_response_serialises_in_camel_case(self) -> None:
        from app.schemas.copilot import CopilotQueryMetadata, CopilotQueryResponse, CopilotSource

        payload = CopilotQueryResponse(
            answer="Thirty days written notice. [1]",
            sources=[
                CopilotSource(
                    contract_id="22222222-2222-2222-2222-222222222222",  # type: ignore[arg-type]
                    contract_name="Acme MSA",
                    clause_heading="Termination",
                    section_number="12.3",
                    page_number=18,
                    similarity_score=0.91,
                )
            ],
            metadata=CopilotQueryMetadata(
                document_type_detected=True,
                document_type_confidence=0.94,
                retrieval_mode="DocumentTypeFiltered",
                retrieved_chunks=8,
                top_similarity=0.91,
            ),
        ).model_dump(mode="json", by_alias=True)

        assert payload["sources"][0]["clauseHeading"] == "Termination"
        assert payload["sources"][0]["sectionNumber"] == "12.3"
        assert payload["sources"][0]["pageNumber"] == 18
        assert payload["sources"][0]["similarityScore"] == pytest.approx(0.91)
        assert payload["metadata"]["documentTypeDetected"] is True
        assert payload["metadata"]["documentTypeConfidence"] == pytest.approx(0.94)
        assert payload["metadata"]["retrievalMode"] == "DocumentTypeFiltered"
        assert payload["metadata"]["retrievedChunks"] == 8
        assert payload["metadata"]["topSimilarity"] == pytest.approx(0.91)


class TestPersistedTurnsLoadBack:
    """A `/query` turn is read back by ``GET /copilot/sessions/{id}``.

    ``chat_messages.citations`` is deserialised as a ``CitationResponse``, so
    storing a turn in the ``CopilotSource`` shape would make the whole conversation
    fail to load - not just that turn.
    """

    def test_a_stored_citation_round_trips(self) -> None:
        import uuid

        from app.ai.retrieval.context import Citation
        from app.api.v1.search import _citation, _rehydrate_citation
        from app.schemas.search import CitationResponse

        stored = _citation(
            Citation(
                label=1,
                contract_id=uuid.uuid4(),
                contract_title="Acme MSA",
                level="chunk",
                ref_id=uuid.uuid4(),
                text="Either party may terminate on thirty days written notice.",
                clause_number="12.3",
                section_title="Termination",
                page_start=18,
                score=0.0164,
                similarity=0.91,
            )
        ).model_dump(mode="json")

        # Exactly what `get_session` does with the stored payload.
        assert CitationResponse(**_rehydrate_citation(stored)).label == 1

    def test_a_source_payload_would_not_round_trip(self) -> None:
        """Pins why the citation shape is stored rather than the source shape."""
        import uuid

        import pytest as _pytest
        from pydantic import ValidationError

        from app.api.v1.search import _rehydrate_citation
        from app.schemas.copilot import CopilotSource
        from app.schemas.search import CitationResponse

        source = CopilotSource(
            contract_id=uuid.uuid4(), contract_name="Acme MSA", similarity_score=0.91
        ).model_dump(mode="json")

        with _pytest.raises(ValidationError):
            CitationResponse(**_rehydrate_citation(source))


class TestNeedsReviewSurvivesReload:
    """A flagged answer must still be flagged when the conversation is reopened.

    ``chat_messages`` had no such column and the reload path read it through
    ``getattr(..., False)``, so every flagged answer presented itself as clean on
    the second viewing. For a repository whose answers are acted on, an audit trail
    asserting an answer was verified when the system knew otherwise is worse than
    one that never made the claim.
    """

    def test_the_column_exists_on_the_model(self) -> None:
        from app.models.chat import ChatMessage

        assert "needs_review" in ChatMessage.__table__.columns

    def test_it_is_not_nullable_and_defaults_to_false(self) -> None:
        from app.models.chat import ChatMessage

        column = ChatMessage.__table__.columns["needs_review"]
        assert column.nullable is False
        assert column.server_default is not None

    def test_the_reload_path_reads_the_column_not_a_default(self) -> None:
        source = (APP_ROOT / "api/v1/search.py").read_text(encoding="utf-8")
        assert 'getattr(message, "needs_review"' not in source
        assert "needs_review=message.needs_review" in source

    def test_both_persist_paths_set_it(self) -> None:
        """`/ask` and `/query` write turns through different helpers; a flag set on
        only one of them is the same bug in half the endpoints."""
        source = (APP_ROOT / "api/v1/search.py").read_text(encoding="utf-8")

        def _body(marker: str) -> str:
            start = source.index(marker)
            return source[start : start + 1400]

        assert "needs_review=answer.needs_review" in _body("async def _persist_turn")
        assert "needs_review=result.needs_review" in _body("async def _persist_query_turn")

    def test_a_migration_adds_it(self) -> None:
        migrations = (APP_ROOT.parent / "migrations" / "versions").glob("*.py")
        assert any("needs_review" in path.read_text(encoding="utf-8") for path in migrations), (
            "the column needs a migration, or an upgraded deployment breaks on write"
        )


class TestRetrievalAuditIsolation:
    """Telemetry must not share a transaction with the compliance audit row.

    A failed flush marks an async session as needing rollback, so a malformed
    telemetry payload could take the access-audit write down with it - trading a
    metrics gap for a hole in the compliance trail.
    """

    def test_it_opens_its_own_session(self) -> None:
        source = (APP_ROOT / "services/retrieval_audit.py").read_text(encoding="utf-8")
        assert "session_scope()" in source

    def test_it_does_not_write_through_the_request_session(self) -> None:
        source = (APP_ROOT / "services/retrieval_audit.py").read_text(encoding="utf-8")
        assert "self.db.add(" not in source
        assert "self.db.flush(" not in source

    def test_query_text_storage_is_configurable(self) -> None:
        """A bank may be unable to retain the text of questions staff asked."""
        from app.services.retrieval_audit import RetrievalAuditService

        assert RetrievalAuditService._query_text("What is the cap?") is not None

    def test_disabling_it_stores_nothing(self, settings_env) -> None:
        settings_env(RETRIEVAL_AUDIT_STORE_QUERY_TEXT="false")

        from app.services.retrieval_audit import RetrievalAuditService

        assert RetrievalAuditService._query_text("What is the cap?") is None

    def test_a_retention_window_is_defined(self) -> None:
        from app.core.config import get_settings

        assert get_settings().retrieval.audit_retention_days > 0

    def test_a_purge_exists(self) -> None:
        from app.services.retrieval_audit import RetrievalAuditService

        assert hasattr(RetrievalAuditService, "purge_expired")


class TestVectorSearchScaling:
    def test_iterative_scan_is_configured(self) -> None:
        """Without it, filters the HNSW index cannot use are applied *after* the
        scan returns its globally-nearest rows - so a large multi-project table
        returns nothing for questions that have a perfect answer in it."""
        from app.core.config import get_settings

        assert get_settings().embedding.hnsw_iterative_scan in {
            "relaxed_order",
            "strict_order",
        }

    def test_the_scan_is_bounded(self) -> None:
        from app.core.config import get_settings

        assert get_settings().embedding.hnsw_max_scan_tuples > 0

    def test_the_parameters_are_set_per_connection(self) -> None:
        source = (APP_ROOT / "db/session.py").read_text(encoding="utf-8")
        for parameter in ("hnsw.ef_search", "hnsw.iterative_scan", "hnsw.max_scan_tuples"):
            assert parameter in source


class TestIngestTablesAreNotReadAtQueryTime:
    """Answers come from the vector store, not from ``cip_DocContentMaster``."""

    @pytest.mark.parametrize("relative", QUERY_PATH_MODULES)
    def test_no_query_path_module_imports_the_ingest_tables(self, relative: str) -> None:
        source = (APP_ROOT / relative).read_text(encoding="utf-8")
        tree = ast.parse(source)

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        offenders = [name for name in imported if "docpipeline.tables" in name]
        assert not offenders, (
            f"{relative} imports {offenders}. The ingest tables must not be read while "
            "answering: everything the answer needs is already on the embedding row."
        )

    @pytest.mark.parametrize("relative", QUERY_PATH_MODULES)
    def test_no_query_path_module_names_the_table(self, relative: str) -> None:
        source = (APP_ROOT / relative).read_text(encoding="utf-8")
        # Comments explaining the rule are fine; a raw SQL string naming the table
        # is not, and would slip past the import check above.
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
        assert "cip_DocContentMaster" not in code


class TestDocumentTypeVocabularyComesFromTheTaxonomy:
    def test_the_service_reads_cip_doc_mapping(self) -> None:
        """The clause taxonomy is data another system owns, so it is read at runtime."""
        source = (APP_ROOT / "services/copilot.py").read_text(encoding="utf-8")
        assert "load_doc_types" in source
        assert "resolve_document_type" in source


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")
class TestLive:
    @pytest.mark.asyncio
    async def test_the_retrieval_audit_table_accepts_a_row(self) -> None:
        """The table has existed since the initial schema and nothing wrote to it."""
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.services.retrieval_audit import RetrievalAuditService

        engine = create_async_engine(TEST_DATABASE_URL or "", future=True)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                await RetrievalAuditService(session).record(
                    operation="copilot",
                    query="What is the termination notice period?",
                    timings={"total_ms": 12},
                )
                count = await session.execute(
                    text(
                        "SELECT count(*) FROM retrieval_audit "
                        "WHERE query_text = 'What is the termination notice period?'"
                    )
                )
                assert count.scalar_one() >= 1
                await session.rollback()
        finally:
            await engine.dispose()

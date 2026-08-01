"""Retrieval audit service - what a Copilot answer retrieved, and how it went (§17).

The ``retrieval_audit`` table has existed since the initial schema and nothing
wrote to it. This is the writer.

It is deliberately separate from :class:`~app.services.audit.AuditService`. That
one records *who did what* for compliance; this records *how retrieval behaved*
for quality: which strategy ran, how many candidates each stage produced, what the
best similarity was, where the milliseconds went, and what the answer cost. Those
are different questions with different retention needs, and putting retrieval
telemetry into the compliance trail would bury the access records an auditor
actually reads.

Like the compliance audit, a failure here is logged and swallowed. Losing a
telemetry row is a metrics gap; failing the user's question because of one would
be a self-inflicted outage.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.versions import retrieval_versions
from app.models.audit import RetrievalAudit

logger = get_logger(__name__)

#: Cap on recorded evidence references. Enough to reconstruct an answer; not so
#: many that one pathological query writes a megabyte of JSONB.
_MAX_EVIDENCE_REFS = 50


class RetrievalAuditService:
    """Writes one row per search or Copilot answer.

    On its **own** session, not the request's. That is the whole reason this class
    holds a session factory rather than a session: a failed ``flush`` marks an
    async SQLAlchemy session as needing rollback, and every subsequent write on it
    fails too. Sharing the request transaction meant one malformed telemetry
    payload could take the *compliance* audit row down with it - trading a metrics
    gap for a hole in the access trail, which is precisely the wrong direction.
    """

    def __init__(self, db: AsyncSession | None = None) -> None:
        #: Only used to read configuration-free state; never written through.
        self._request_session = db

    async def record(
        self,
        *,
        operation: str,
        query: str,
        project_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
        message_id: uuid.UUID | None = None,
        plan: Any = None,
        retrieval: Any = None,
        package: Any = None,
        answer: Any = None,
        analysis: Any = None,
        timings: dict[str, int] | None = None,
        cache_hit: bool = False,
    ) -> None:
        """Record a retrieval. Never raises, and never touches the caller's session."""
        try:
            timings = timings or {}
            row = RetrievalAudit(
                project_id=project_id,
                user_id=user_id,
                session_id=session_id,
                message_id=message_id,
                operation=operation,
                query_text=self._query_text(query),
                query_intent=plan.intent.value if plan is not None else None,
                scope=plan.scope.value if plan is not None else None,
                strategy=plan.strategy.value if plan is not None else None,
                filters=self._filters(plan, analysis),
                candidate_counts=self._counts(retrieval),
                evidence_refs=self._evidence_refs(package),
                graph_depth=getattr(plan, "graph_depth", None),
                retrieval_ms=timings.get("retrieval_ms"),
                rerank_ms=timings.get("rerank_ms"),
                inference_ms=timings.get("inference_ms"),
                total_ms=timings.get("total_ms"),
                result_count=len(retrieval.evidence) if retrieval is not None else None,
                citation_count=len(answer.citations) if answer is not None else None,
                citation_coverage=self._coverage(package, answer),
                confidence=self._decimal(getattr(answer, "confidence", None), places=4),
                versions=retrieval_versions().model_dump(mode="json", exclude_none=True),
                token_usage=answer.usage.as_dict() if answer is not None else {},
                cost_usd=self._decimal(getattr(answer, "cost_usd", None), places=6),
                validation_result=self._validation(answer),
                cache_hit=cache_hit,
            )

            from app.db.session import session_scope

            # Its own transaction, committed independently. A telemetry write must
            # not be able to roll back - or poison - the transaction carrying the
            # user's chat turn and the compliance audit row.
            async with session_scope() as session:
                session.add(row)
        except Exception as exc:  # noqa: BLE001 - telemetry must not fail a question
            logger.warning("retrieval_audit_failed", operation=operation, error=str(exc))

    @staticmethod
    def _query_text(query: str) -> str | None:
        """The recorded query, subject to the deployment's retention policy.

        A bank may be unable to retain the text of questions its staff asked about
        contracts - the query itself can carry counterparty names and deal terms.
        Storage is therefore opt-out, and every other field on the row stays useful
        without it: strategy, similarity, latency and cost are what the retrieval
        metrics are actually built from.
        """
        if not query or not get_settings().retrieval.audit_store_query_text:
            return None
        return query[:4000]

    # =========================================================================
    # Payload builders
    # =========================================================================
    @staticmethod
    def _filters(plan: Any, analysis: Any) -> dict[str, Any]:
        """The filters that ran, plus why the document-type one did or did not.

        The classifier's verdict is recorded even when it was discarded: "the type
        was detected at 0.6 and the threshold is 0.75" is the single most useful
        thing to know when tuning that threshold, and it is unrecoverable after the
        fact if only the applied filters are stored.
        """
        payload: dict[str, Any] = dict(plan.filters.as_dict()) if plan is not None else {}
        if analysis is not None:
            payload["document_type_analysis"] = analysis.as_dict()
        return payload

    @staticmethod
    def _counts(retrieval: Any) -> dict[str, Any]:
        if retrieval is None:
            return {}
        counts: dict[str, Any] = {
            "candidates": len(retrieval.candidate_contracts),
            "metadata_rows": len(retrieval.metadata_rows),
            "top_similarity": round(retrieval.top_similarity, 6),
        }
        for item in retrieval.evidence:
            key = item.level.value
            counts[key] = int(counts.get(key, 0)) + 1
        counts["by_source"] = dict(retrieval.counts)
        return counts

    @staticmethod
    def _evidence_refs(package: Any) -> dict[str, Any]:
        """Exactly what the model was shown, so an answer can be reconstructed."""
        if package is None:
            return {}
        return {
            "citations": [
                {
                    "label": citation.label,
                    "level": citation.level,
                    "ref_id": str(citation.ref_id),
                    "contract_id": str(citation.contract_id),
                    "similarity": (
                        round(citation.similarity, 6) if citation.similarity is not None else None
                    ),
                }
                for citation in package.citations[:_MAX_EVIDENCE_REFS]
            ],
            "dropped": package.dropped,
            "token_estimate": package.token_estimate,
        }

    @staticmethod
    def _validation(answer: Any) -> dict[str, Any]:
        if answer is None:
            return {}
        return {
            "refused": answer.refused,
            "needs_review": answer.needs_review,
            "invalid_citations": list(answer.invalid_citations),
            "confidence_band": answer.confidence_band.value,
            "warnings": list(answer.warnings)[:10],
        }

    @staticmethod
    def _coverage(package: Any, answer: Any) -> Decimal | None:
        """Fraction of the offered evidence the answer actually cited.

        The retrieval-quality signal: persistently low coverage means retrieval is
        returning passages the answer has no use for, which costs context budget
        and dilutes what the model reads.
        """
        if package is None or answer is None or not package.citations:
            return None
        return RetrievalAuditService._decimal(
            len(answer.citations) / len(package.citations), places=4
        )

    @staticmethod
    def _decimal(value: Any, *, places: int) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(round(float(value), places)))
        except (TypeError, ValueError, ArithmeticError):
            return None

    @staticmethod
    async def purge_expired() -> int:
        """Delete audit rows past the retention window. Returns the row count.

        Run on a schedule rather than at write time: a delete on the hot path adds
        latency to every question to solve a problem that is not urgent on any
        single request.
        """
        from sqlalchemy import delete

        from app.db.session import session_scope

        days = get_settings().retrieval.audit_retention_days
        cutoff = datetime.now(UTC) - timedelta(days=days)

        async with session_scope() as session:
            result = await session.execute(
                delete(RetrievalAudit).where(RetrievalAudit.created_at < cutoff)
            )
        # `rowcount` is on the cursor result a DELETE returns, not on the generic
        # `Result` the type stubs describe.
        deleted = int(getattr(result, "rowcount", 0) or 0)
        logger.info("retrieval_audit_purged", deleted=deleted, retention_days=days)
        return deleted


__all__ = ["RetrievalAuditService"]

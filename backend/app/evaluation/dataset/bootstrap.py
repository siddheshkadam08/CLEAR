"""Bootstrapping a golden dataset from a real project.

Writing a thousand golden cases by hand is the reason most teams never build a
benchmark. Half that work is mechanical - finding the contracts, reading out
clause ids, headings and page numbers - and that half is what this does.

**It does not write the questions.** A generated question tests whatever the
generator understood about the clause, which is circular: it would score the
retrieval pipeline against a paraphrase produced from the same text the pipeline
is being asked to find. The stubs come out with a placeholder marker so an
unedited dataset is obvious, and :func:`validate` refuses to score one.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.core.logging import get_logger
from app.evaluation.dataset.models import GoldenCase, GoldenDataset, GoldenExpectation

logger = get_logger(__name__)

#: Marks a question nobody has written yet. Present in the file, checked by the
#: loader's warnings, and impossible to miss in review.
PLACEHOLDER = "TODO: write the question"


async def generate_skeleton(
    *,
    project_id: uuid.UUID,
    name: str = "generated",
    per_contract: int = 3,
    contract_limit: int = 50,
) -> GoldenDataset:
    """Build case stubs from the contracts and clauses a project holds.

    Clauses are chosen by taking the distinct clause *types* present in each
    contract rather than the first N rows. A dataset built from the first three
    clauses of every contract would be almost entirely definitions and parties -
    the front matter - and would score retrieval on the least interesting text in
    the corpus.
    """
    from app.db.session import session_scope
    from app.models.contract import Contract
    from app.models.knowledge import Clause

    dataset = GoldenDataset(
        name=name,
        version="1",
        description=(
            f"Skeleton generated from project {project_id}. Every question is a "
            "placeholder and must be written by hand before this scores anything."
        ),
        tags=["generated"],
    )

    async with session_scope() as session:
        contracts = (
            (
                await session.execute(
                    select(Contract)
                    .where(Contract.project_id == project_id, Contract.deleted_at.is_(None))
                    .order_by(Contract.created_at)
                    .limit(contract_limit)
                )
            )
            .scalars()
            .all()
        )

        for contract in contracts:
            clauses = (
                (
                    await session.execute(
                        select(Clause)
                        .where(
                            Clause.contract_id == contract.id,
                            Clause.project_id == project_id,
                        )
                        .order_by(Clause.page_start, Clause.clause_number)
                    )
                )
                .scalars()
                .all()
            )
            if not clauses:
                continue

            for index, clause in enumerate(_diverse(list(clauses), per_contract), start=1):
                dataset.cases.append(
                    GoldenCase(
                        id=f"{contract.id.hex[:8]}-{index}",
                        question=(
                            f"{PLACEHOLDER} about "
                            f"{(clause.clause_type or 'this clause').replace('_', ' ')} "
                            f"in {contract.title or 'this contract'}"
                        ),
                        project_id=project_id,
                        expected=GoldenExpectation(
                            contracts=[contract.id],
                            clauses=[clause.id],
                            pages=[clause.page_start] if clause.page_start else [],
                            headings=[clause.section_title] if clause.section_title else [],
                            agreement_types=(
                                [contract.agreement_type] if contract.agreement_type else []
                            ),
                            should_answer=True,
                        ),
                        tags=[tag for tag in (contract.agreement_type, clause.clause_type) if tag],
                        notes=(
                            f"Clause {clause.clause_number or '?'} "
                            f"p.{clause.page_start or '?'}: "
                            f"{(clause.text_content or '')[:160]}"
                        ),
                    )
                )

    logger.info(
        "dataset_skeleton_generated",
        project_id=str(project_id),
        contracts=len(contracts),
        cases=len(dataset.cases),
    )
    return dataset


def _diverse(clauses: list, limit: int) -> list:
    """One clause per distinct type, then fill from what is left.

    Diversity first, because a benchmark that only asks about the clause types a
    contract happens to lead with measures the front matter.
    """
    seen: set[str] = set()
    chosen: list = []

    for clause in clauses:
        key = clause.clause_type or ""
        if key and key not in seen:
            seen.add(key)
            chosen.append(clause)
        if len(chosen) >= limit:
            return chosen

    for clause in clauses:
        if clause not in chosen:
            chosen.append(clause)
        if len(chosen) >= limit:
            break
    return chosen


def has_placeholders(dataset: GoldenDataset) -> list[str]:
    """Case ids whose questions were never written."""
    return [case.id for case in dataset.cases if PLACEHOLDER in case.question]


__all__ = ["PLACEHOLDER", "generate_skeleton", "has_placeholders"]

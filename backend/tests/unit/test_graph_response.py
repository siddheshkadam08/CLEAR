"""The graph response schemas must match what the builder actually emits.

``GET /contracts/{id}/graph`` is a thin adapter: it calls
:class:`~app.ai.graph.builder.KnowledgeGraphBuilder`, then splats each
``as_dict()`` into a response model. That is only safe while the two agree about
field names, and nothing else checks that they do - the endpoint would have kept
returning 200 with a silently empty ``dangling`` list if they drifted, which is
precisely how the first version of :class:`DanglingReference` was wrong: it
invented ``source_ref``/``target_ref`` fields, and the builder emits
``reference``/``relation``/``reason``.

So these tests do not construct dictionaries by hand. They run the real builder
over row-shaped stubs and feed its real output into the real schemas, which is the
only version of this test that can catch the drift.

The dangling case gets the most attention because it is the one worth having: an
unresolved reference means the document cites a schedule or clause that is not in
the repository, and a viewer that dropped those would present a tidier graph than
the truth.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.ai.graph import KnowledgeGraphBuilder
from app.schemas.graph import (
    ContractGraphResponse,
    DanglingReference,
    GraphEdgeResponse,
    GraphNodeResponse,
)


class Row:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


CONTRACT_ID = uuid.UUID("aaaa0000-0000-4000-8000-000000000003")


def _clause(number: str, clause_type: str, text: str) -> Row:
    return Row(
        id=uuid.uuid4(),
        clause_number=number,
        clause_type=clause_type,
        title=clause_type.replace("_", " ").title(),
        text=text,
        is_mandatory=True,
        is_risk_flagged=False,
    )


def _build() -> Any:
    """A contract whose clauses cite each other, and cite things that do not exist."""
    return KnowledgeGraphBuilder().build(
        contract_id=CONTRACT_ID,
        contract_title="Umbrella Logistics NDA",
        agreement_type="nda",
        parties=[
            Row(id=uuid.uuid4(), name="Globex", role="counterparty", is_primary=True, aliases=[]),
        ],
        clauses=[
            _clause(
                "3",
                "confidentiality",
                "Subject to the exceptions in Section 4 and the term in Clause 7.",
            ),
            _clause("4", "exceptions", "Section 3 shall not apply to public information."),
            _clause("7", "term", "Termination is governed by Section 8."),
        ],
        obligations=[
            Row(
                id=uuid.uuid4(),
                action="Return all Confidential Information",
                responsible_party="Globex",
                due_date=None,
                status="open",
            )
        ],
        risks=[
            Row(
                id=uuid.uuid4(),
                risk_type="unlimited_liability",
                severity="critical",
                description="Liability is uncapped.",
                is_omission=False,
            )
        ],
        relationships=[],
        metadata=None,
    )


def test_every_node_fits_the_response_model() -> None:
    graph = _build()
    assert graph.nodes, "the builder produced no nodes, so this asserts nothing"
    for node in graph.nodes:
        GraphNodeResponse(**node.as_dict())


def test_every_edge_fits_the_response_model() -> None:
    graph = _build()
    assert graph.edges, "the builder produced no edges, so this asserts nothing"
    for edge in graph.edges:
        GraphEdgeResponse(**edge.as_dict())


def test_dangling_entries_fit_the_response_model() -> None:
    """The drift that shipped once. "Section 8" resolves to nothing."""
    graph = _build()
    assert graph.dangling, "no dangling references were produced - the fixture is wrong"

    parsed = [DanglingReference(**entry) for entry in graph.dangling]
    assert any(entry.reference == "Section 8" for entry in parsed), (
        f"expected 'Section 8' among {[entry.reference for entry in parsed]}"
    )
    # The reason is what makes the finding actionable rather than a bare warning.
    assert all(entry.reason for entry in parsed)


def test_cross_references_between_clauses_resolve() -> None:
    graph = _build()
    references = [edge for edge in graph.edges if edge.relation.value == "references"]
    pairs = {(edge.source_ref, edge.target_ref) for edge in references}
    assert ("3", "4") in pairs
    assert ("3", "7") in pairs
    assert ("4", "3") in pairs
    assert all(edge.is_resolved for edge in references)


def test_the_whole_response_assembles() -> None:
    """Exactly what the endpoint does, so a field rename fails here too."""
    graph = _build()
    response = ContractGraphResponse(
        contract_id=CONTRACT_ID,
        contract_title="Umbrella Logistics NDA",
        nodes=[GraphNodeResponse(**node.as_dict()) for node in graph.nodes],
        edges=[GraphEdgeResponse(**edge.as_dict()) for edge in graph.edges],
        dangling=[DanglingReference(**entry) for entry in graph.dangling],
        statistics=graph.statistics(),
        warnings=list(graph.warnings),
    )

    assert response.statistics["nodes"] == len(response.nodes)
    assert response.statistics["edges"] == len(response.edges)
    assert response.statistics["dangling_references"] == len(response.dangling)
    # An unresolved reference is reported to the reader, not just counted.
    assert response.warnings

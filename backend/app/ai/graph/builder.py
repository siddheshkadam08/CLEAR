"""Knowledge graph builder (§14).

Turns a contract's extracted knowledge into a resolved graph: nodes for the contract,
its parties, its clauses, its obligations and its risks, and edges for the
relationships between them.

Two jobs, and the second is the one that matters:

* **Derive the structural edges.** A contract *contains* its clauses, is *governed by*
  a law, is *assigned to* an owner, and its clauses *depend on* the obligations they
  create. These follow from the extracted rows and need no model call.
* **Resolve the extracted references.** Extraction records what the text says -
  "as set out in Section 9", "the Supplier shall" - as unresolved edges with string
  references. Resolution turns ``"Section 9"`` into the clause row it names, and
  ``"the Supplier"`` into the party row. An unresolved edge is kept, not discarded:
  a reference to a schedule that was never uploaded is a real finding, and silently
  dropping it would hide a gap in the repository.

**Project isolation is absolute here (§1.1).** A graph is built per contract and
traversed within one project. There is no cross-project edge, and no traversal in
this module can widen scope - a graph query that followed an edge into another
project would be the easiest possible way to breach the boundary.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import GraphNodeType, GraphRelation, RiskSeverity
from app.core.logging import get_logger

logger = get_logger(__name__)

#: "Section 9", "clause 11.2", "Article IV" - how contracts cross-reference.
_SECTION_REFERENCE = re.compile(
    r"\b(?:section|clause|article|paragraph|schedule|exhibit|annex(?:ure)?|appendix)\s+"
    r"(?P<number>[0-9]+(?:\.[0-9]+)*|[IVXLCDM]+|[A-Z])\b",
    re.IGNORECASE,
)

#: Definite-article party references a contract uses after defining a short form.
_PARTY_ARTICLE = re.compile(r"^\s*the\s+", re.IGNORECASE)


@dataclass(slots=True)
class GraphNode:
    """One node. Identified by ``(node_type, ref)`` within a project."""

    node_type: GraphNodeType
    #: Stable reference within the contract: a clause number, a party name, an id.
    ref: str
    label: str
    #: The database row this node stands for, when it has one.
    row_id: uuid.UUID | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.node_type.value, self.ref)

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_type": self.node_type.value,
            "ref": self.ref,
            "label": self.label,
            "row_id": str(self.row_id) if self.row_id else None,
            "attributes": self.attributes,
        }


@dataclass(slots=True)
class GraphEdge:
    """One directed edge."""

    relation: GraphRelation
    source_type: str
    source_ref: str
    target_type: str
    target_ref: str
    label: str | None = None
    source_id: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    #: True when both endpoints resolved to a known node.
    is_resolved: bool = False
    #: Where this edge came from: ``derived`` (structural) or ``extracted`` (the text
    #: said so). Kept because a derived edge is a fact about our data model and an
    #: extracted one is a claim about the document.
    origin: str = "derived"

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.relation.value,
            self.source_type,
            self.source_ref,
            self.target_type,
            self.target_ref,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation.value,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "target_type": self.target_type,
            "target_ref": self.target_ref,
            "label": self.label,
            "source_id": str(self.source_id) if self.source_id else None,
            "target_id": str(self.target_id) if self.target_id else None,
            "attributes": self.attributes,
            "is_resolved": self.is_resolved,
            "origin": self.origin,
        }


@dataclass(slots=True)
class GraphResult:
    """The built graph."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    #: References the text made that no node satisfies - a schedule never uploaded,
    #: a clause number that does not exist. Surfaced, never swallowed.
    dangling: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def resolved_edges(self) -> int:
        return sum(1 for edge in self.edges if edge.is_resolved)

    def statistics(self) -> dict[str, Any]:
        by_node: dict[str, int] = {}
        for node in self.nodes:
            by_node[node.node_type.value] = by_node.get(node.node_type.value, 0) + 1
        by_relation: dict[str, int] = {}
        for edge in self.edges:
            by_relation[edge.relation.value] = by_relation.get(edge.relation.value, 0) + 1
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "resolved_edges": self.resolved_edges,
            "unresolved_edges": len(self.edges) - self.resolved_edges,
            "dangling_references": len(self.dangling),
            "by_node_type": by_node,
            "by_relation": by_relation,
        }


class KnowledgeGraphBuilder:
    """Builds one contract's graph from its extracted rows.

    Pure: takes rows in, returns nodes and edges. No session, no writes - the indexing
    stage persists the result.
    """

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, str], GraphNode] = {}
        self._edges: dict[tuple[str, str, str, str, str], GraphEdge] = {}
        self._dangling: list[dict[str, str]] = []
        #: Lowercased party name/alias -> the party node's ref, for resolving
        #: "the Supplier" back to a real entity.
        self._party_index: dict[str, str] = {}
        #: Clause number -> clause node ref, for resolving "Section 9".
        self._clause_index: dict[str, str] = {}

    def build(
        self,
        *,
        contract_id: uuid.UUID,
        contract_title: str | None,
        agreement_type: str | None,
        parties: list[Any],
        clauses: list[Any],
        obligations: list[Any],
        risks: list[Any],
        relationships: list[Any],
        metadata: Any = None,
    ) -> GraphResult:
        self._nodes.clear()
        self._edges.clear()
        self._dangling.clear()
        self._party_index.clear()
        self._clause_index.clear()

        contract_ref = str(contract_id)
        self._add_node(
            GraphNode(
                node_type=GraphNodeType.CONTRACT,
                ref=contract_ref,
                label=contract_title or "Contract",
                row_id=contract_id,
                attributes={
                    "agreement_type": agreement_type,
                    "risk_band": getattr(metadata, "risk_band", None),
                    "risk_score": getattr(metadata, "risk_score", None),
                },
            )
        )

        self._add_parties(contract_ref, parties)
        self._add_clauses(contract_ref, clauses)
        self._add_obligations(contract_ref, obligations)
        self._add_risks(contract_ref, risks)
        self._add_governing_law(contract_ref, metadata, clauses)
        self._resolve_extracted(contract_ref, relationships)
        self._link_clause_cross_references(clauses)

        result = GraphResult(
            nodes=list(self._nodes.values()),
            edges=list(self._edges.values()),
            dangling=list(self._dangling),
        )

        if result.dangling:
            result.warnings.append(
                f"{len(result.dangling)} reference(s) in the text could not be resolved "
                "to anything in this contract - they may point at a schedule or "
                "agreement that has not been uploaded."
            )

        logger.info(
            "knowledge_graph_built",
            contract_id=contract_ref,
            **result.statistics(),
        )
        return result

    # =========================================================================
    # Nodes and structural edges
    # =========================================================================
    def _add_parties(self, contract_ref: str, parties: list[Any]) -> None:
        for party in parties:
            name = str(getattr(party, "name", "") or "").strip()
            if not name:
                continue

            role = getattr(party, "role", None)
            node_type = _party_node_type(role)
            node = GraphNode(
                node_type=node_type,
                ref=name,
                label=name,
                row_id=getattr(party, "id", None),
                attributes={
                    "role": role,
                    "is_primary": bool(getattr(party, "is_primary", False)),
                    "jurisdiction": getattr(party, "jurisdiction", None),
                },
            )
            self._add_node(node)

            # Index the name and every defined alias, because later clauses refer to
            # parties by their short form ("the Supplier") rather than their name.
            self._party_index[name.lower()] = name
            for alias in getattr(party, "aliases", None) or []:
                alias_text = str(alias).strip().lower()
                if alias_text:
                    self._party_index[alias_text] = name
                    self._party_index[_PARTY_ARTICLE.sub("", alias_text)] = name

            self._add_edge(
                GraphEdge(
                    relation=GraphRelation.BELONGS_TO,
                    source_type=node_type.value,
                    source_ref=name,
                    target_type=GraphNodeType.CONTRACT.value,
                    target_ref=contract_ref,
                    label=role,
                    source_id=getattr(party, "id", None),
                    target_id=uuid.UUID(contract_ref),
                    is_resolved=True,
                )
            )

    def _add_clauses(self, contract_ref: str, clauses: list[Any]) -> None:
        for clause in clauses:
            clause_type = str(getattr(clause, "clause_type", "") or "")
            number = str(getattr(clause, "clause_number", "") or "").strip()
            # Prefer the printed number as the reference - it is what the document
            # itself cites - falling back to the type when the clause is unnumbered.
            ref = number or clause_type
            if not ref:
                continue

            node = GraphNode(
                node_type=GraphNodeType.CLAUSE,
                ref=ref,
                label=str(getattr(clause, "title", None) or clause_type.replace("_", " ")),
                row_id=getattr(clause, "id", None),
                attributes={
                    "clause_type": clause_type,
                    "clause_number": number or None,
                    "is_risk_flagged": bool(getattr(clause, "is_risk_flagged", False)),
                    "page_start": getattr(clause, "page_start", None),
                    # The clause's own extracted attributes, nested rather than
                    # merged: a clause attribute called `clause_type` would otherwise
                    # overwrite the node's. They are carried because they are what
                    # makes a clause node answerable - "which contracts cap at 1x"
                    # is a graph question, and without these the node is a label.
                    "extracted": dict(getattr(clause, "attributes", None) or {}),
                },
            )
            self._add_node(node)
            if number:
                self._clause_index[_normalise_number(number)] = ref

            self._add_edge(
                GraphEdge(
                    relation=GraphRelation.CONTAINS,
                    source_type=GraphNodeType.CONTRACT.value,
                    source_ref=contract_ref,
                    target_type=GraphNodeType.CLAUSE.value,
                    target_ref=ref,
                    label=clause_type,
                    source_id=uuid.UUID(contract_ref),
                    target_id=getattr(clause, "id", None),
                    is_resolved=True,
                )
            )

    def _add_obligations(self, contract_ref: str, obligations: list[Any]) -> None:
        for index, obligation in enumerate(obligations):
            action = str(getattr(obligation, "action", "") or "").strip()
            if not action:
                continue
            ref = f"obligation-{index}"
            self._add_node(
                GraphNode(
                    node_type=GraphNodeType.OBLIGATION,
                    ref=ref,
                    label=action[:120],
                    row_id=getattr(obligation, "id", None),
                    attributes={
                        "responsible_party": getattr(obligation, "responsible_party", None),
                        "due_date": _iso(getattr(obligation, "due_date", None)),
                        "due_description": getattr(obligation, "due_description", None),
                        "is_recurring": bool(getattr(obligation, "is_recurring", False)),
                    },
                )
            )
            self._add_edge(
                GraphEdge(
                    relation=GraphRelation.CONTAINS,
                    source_type=GraphNodeType.CONTRACT.value,
                    source_ref=contract_ref,
                    target_type=GraphNodeType.OBLIGATION.value,
                    target_ref=ref,
                    source_id=uuid.UUID(contract_ref),
                    target_id=getattr(obligation, "id", None),
                    is_resolved=True,
                )
            )

            # Who owes it. Resolved through the party index so "the Supplier" links to
            # the entity rather than becoming a second, phantom party.
            party = self._resolve_party(getattr(obligation, "responsible_party", None))
            if party is not None:
                self._add_edge(
                    GraphEdge(
                        relation=GraphRelation.ASSIGNED_TO,
                        source_type=GraphNodeType.OBLIGATION.value,
                        source_ref=ref,
                        target_type=party.node_type.value,
                        target_ref=party.ref,
                        source_id=getattr(obligation, "id", None),
                        target_id=party.row_id,
                        is_resolved=True,
                    )
                )

    def _add_risks(self, contract_ref: str, risks: list[Any]) -> None:
        for index, risk in enumerate(risks):
            risk_type = str(getattr(risk, "risk_type", "") or "")
            if not risk_type:
                continue
            ref = f"risk-{index}-{risk_type}"
            severity = getattr(risk, "severity", None)
            self._add_node(
                GraphNode(
                    node_type=GraphNodeType.RISK,
                    ref=ref,
                    label=str(getattr(risk, "description", "") or risk_type)[:120],
                    row_id=getattr(risk, "id", None),
                    attributes={
                        "risk_type": risk_type,
                        "severity": severity.value
                        if isinstance(severity, RiskSeverity)
                        else str(severity or ""),
                        "is_omission": bool(getattr(risk, "is_omission", False)),
                        "clause_type": getattr(risk, "clause_type", None),
                    },
                )
            )
            self._add_edge(
                GraphEdge(
                    relation=GraphRelation.CONTAINS,
                    source_type=GraphNodeType.CONTRACT.value,
                    source_ref=contract_ref,
                    target_type=GraphNodeType.RISK.value,
                    target_ref=ref,
                    source_id=uuid.UUID(contract_ref),
                    target_id=getattr(risk, "id", None),
                    is_resolved=True,
                )
            )

            # Attach the risk to the clause that caused it, where there is one. An
            # omission has no clause by definition, and forcing an edge would invent
            # a link to a clause that does not exist.
            clause_node = self._clause_by_type(getattr(risk, "clause_type", None))
            if clause_node is not None and not getattr(risk, "is_omission", False):
                self._add_edge(
                    GraphEdge(
                        relation=GraphRelation.DEPENDS_ON,
                        source_type=GraphNodeType.RISK.value,
                        source_ref=ref,
                        target_type=GraphNodeType.CLAUSE.value,
                        target_ref=clause_node.ref,
                        source_id=getattr(risk, "id", None),
                        target_id=clause_node.row_id,
                        is_resolved=True,
                    )
                )

    def _add_governing_law(self, contract_ref: str, metadata: Any, clauses: list[Any]) -> None:
        """Link the contract to the law that governs it.

        A definition node rather than a party: the governing law is a named thing the
        contract points at, and modelling it as a node makes "every contract governed
        by Delaware law" a graph query instead of a string scan.
        """
        law = getattr(metadata, "governing_law", None)
        if not law:
            # Fall back to the clause's own extracted attributes: the metadata
            # projection may not have been written yet when indexing runs.
            clause = self._clause_by_type("governing_law")
            if clause is not None:
                extracted = clause.attributes.get("extracted") or {}
                law = extracted.get("governing_law") or extracted.get("country")
        law_text = str(law or "").strip()
        if not law_text:
            return

        self._add_node(
            GraphNode(
                node_type=GraphNodeType.DEFINITION,
                ref=law_text,
                label=law_text,
                attributes={"kind": "governing_law"},
            )
        )
        self._add_edge(
            GraphEdge(
                relation=GraphRelation.GOVERNED_BY,
                source_type=GraphNodeType.CONTRACT.value,
                source_ref=contract_ref,
                target_type=GraphNodeType.DEFINITION.value,
                target_ref=law_text,
                source_id=uuid.UUID(contract_ref),
                is_resolved=True,
            )
        )

    # =========================================================================
    # Resolution
    # =========================================================================
    def _resolve_extracted(self, contract_ref: str, relationships: list[Any]) -> None:
        """Turn extraction's string references into resolved edges where possible."""
        for relationship in relationships:
            relation = _coerce_relation(getattr(relationship, "relation", None))
            if relation is None:
                continue

            source_ref = str(getattr(relationship, "source_ref", "") or "").strip()
            target_ref = str(getattr(relationship, "target_ref", "") or "").strip()
            if not (source_ref and target_ref):
                continue

            source = self._resolve_reference(
                source_ref, str(getattr(relationship, "source_type", "") or "")
            )
            target = self._resolve_reference(
                target_ref, str(getattr(relationship, "target_type", "") or "")
            )

            resolved = source is not None and target is not None
            if not resolved:
                for ref, node in ((source_ref, source), (target_ref, target)):
                    if node is None:
                        self._dangling.append(
                            {
                                "reference": ref,
                                "relation": relation.value,
                                "reason": "no node in this contract matches this reference",
                            }
                        )

            self._add_edge(
                GraphEdge(
                    relation=relation,
                    source_type=source.node_type.value
                    if source
                    else str(getattr(relationship, "source_type", "") or "term"),
                    source_ref=source.ref if source else source_ref,
                    target_type=target.node_type.value
                    if target
                    else str(getattr(relationship, "target_type", "") or "term"),
                    target_ref=target.ref if target else target_ref,
                    label=getattr(relationship, "label", None),
                    source_id=source.row_id if source else None,
                    target_id=target.row_id if target else None,
                    is_resolved=resolved,
                    origin="extracted",
                    attributes=dict(getattr(relationship, "attributes", None) or {}),
                )
            )

    def _link_clause_cross_references(self, clauses: list[Any]) -> None:
        """Detect "as set out in Section 9" inside clause text and link it.

        Deterministic and free - a regex over text already in hand - and it catches
        the references extraction did not report. A clause that cannot be read without
        the one it points at is exactly what hierarchical retrieval needs to know.
        """
        for clause in clauses:
            text = str(getattr(clause, "text_content", "") or getattr(clause, "text", "") or "")
            if not text:
                continue
            own_number = _normalise_number(str(getattr(clause, "clause_number", "") or ""))
            source = self._clause_node_for(clause)
            if source is None:
                continue

            for match in _SECTION_REFERENCE.finditer(text):
                number = _normalise_number(match.group("number"))
                if not number or number == own_number:
                    # A clause referring to itself is a drafting artefact, not an edge.
                    continue
                target_ref = self._clause_index.get(number)
                if target_ref is None:
                    self._dangling.append(
                        {
                            "reference": match.group(0),
                            "relation": GraphRelation.REFERENCES.value,
                            "reason": "the referenced clause was not extracted",
                        }
                    )
                    continue
                target = self._nodes.get((GraphNodeType.CLAUSE.value, target_ref))
                self._add_edge(
                    GraphEdge(
                        relation=GraphRelation.REFERENCES,
                        source_type=GraphNodeType.CLAUSE.value,
                        source_ref=source.ref,
                        target_type=GraphNodeType.CLAUSE.value,
                        target_ref=target_ref,
                        label=match.group(0),
                        source_id=source.row_id,
                        target_id=target.row_id if target else None,
                        is_resolved=True,
                        origin="derived",
                    )
                )

    def _resolve_reference(self, ref: str, hinted_type: str) -> GraphNode | None:
        """Find the node a textual reference names.

        Tries the hinted type first, then a party lookup, then a clause number. The
        hint is a hint: extraction reports what it thinks the reference is, and being
        wrong about the type should not prevent resolution.
        """
        lowered = ref.strip().lower()

        if hinted_type in {"party", "vendor", "customer"} or lowered in self._party_index:
            party = self._resolve_party(ref)
            if party is not None:
                return party

        match = _SECTION_REFERENCE.search(ref)
        number = _normalise_number(match.group("number") if match else ref)
        clause_ref = self._clause_index.get(number)
        if clause_ref:
            return self._nodes.get((GraphNodeType.CLAUSE.value, clause_ref))

        # An exact node ref, whatever its type.
        for node in self._nodes.values():
            if node.ref.lower() == lowered:
                return node
        return None

    def _resolve_party(self, name: str | None) -> GraphNode | None:
        if not name:
            return None
        lowered = str(name).strip().lower()
        candidate = self._party_index.get(lowered) or self._party_index.get(
            _PARTY_ARTICLE.sub("", lowered)
        )
        if candidate is None:
            # Substring match, for "Acme Corporation Ltd" against "Acme Corporation".
            for known, canonical in self._party_index.items():
                if known and (known in lowered or lowered in known):
                    candidate = canonical
                    break
        if candidate is None:
            return None
        for node_type in (
            GraphNodeType.PARTY,
            GraphNodeType.VENDOR,
            GraphNodeType.CUSTOMER,
        ):
            node = self._nodes.get((node_type.value, candidate))
            if node is not None:
                return node
        return None

    def _clause_by_type(self, clause_type: str | None) -> GraphNode | None:
        if not clause_type:
            return None
        for node in self._nodes.values():
            if (
                node.node_type is GraphNodeType.CLAUSE
                and node.attributes.get("clause_type") == clause_type
            ):
                return node
        return None

    def _clause_node_for(self, clause: Any) -> GraphNode | None:
        number = str(getattr(clause, "clause_number", "") or "").strip()
        clause_type = str(getattr(clause, "clause_type", "") or "")
        return self._nodes.get((GraphNodeType.CLAUSE.value, number or clause_type))

    # =========================================================================
    # Collection
    # =========================================================================
    def _add_node(self, node: GraphNode) -> None:
        """Add a node, keeping the first of any duplicate.

        First wins deliberately: the structural pass runs before resolution, so an
        earlier node carries a database id that a later inferred one would not.
        """
        self._nodes.setdefault(node.key, node)

    def _add_edge(self, edge: GraphEdge) -> None:
        """Add an edge, keeping the better of any duplicate.

        Two sources can find the same edge: the model reports "as set out in Section
        9" as an extracted relationship, and the deterministic cross-reference scan
        finds the same wording in the clause text. That is one edge, not two - but the
        agreement is worth recording, because an edge both a model and a regex found
        is more trustworthy than either alone, and the retrieval planner can weight it.
        """
        existing = self._edges.get(edge.key)
        if existing is None:
            self._edges[edge.key] = edge
            return

        corroborated = existing.origin != edge.origin
        # A resolved edge beats an unresolved one; otherwise the first stands.
        winner = edge if (edge.is_resolved and not existing.is_resolved) else existing
        if corroborated:
            winner.attributes = {**winner.attributes, "corroborated": True}
            winner.origin = "extracted+derived"
        self._edges[edge.key] = winner


# =============================================================================
# Helpers
# =============================================================================
def _party_node_type(role: str | None) -> GraphNodeType:
    """Map a party role onto a node type.

    Vendors and customers are distinct node types because "show me everything from
    this vendor" is a first-class question in the repository view.
    """
    if not role:
        return GraphNodeType.PARTY
    lowered = role.lower()
    if lowered in {"vendor", "supplier", "service_provider", "licensor", "contractor"}:
        return GraphNodeType.VENDOR
    if lowered in {"customer", "licensee", "tenant", "employer"}:
        return GraphNodeType.CUSTOMER
    return GraphNodeType.PARTY


def _coerce_relation(value: Any) -> GraphRelation | None:
    """Coerce a relation to the enum, or None.

    Extraction output is data: an unrecognised relation is dropped with the edge
    rather than raising, because losing one edge is better than losing the graph.
    """
    if isinstance(value, GraphRelation):
        return value
    if isinstance(value, str):
        try:
            return GraphRelation(value.strip().lower())
        except ValueError:
            logger.debug("unknown_graph_relation", relation=value)
    return None


def _normalise_number(value: str) -> str:
    """Normalise a clause number for matching: strip trailing punctuation and case."""
    return value.strip().rstrip(".)").upper()


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


__all__ = ["GraphEdge", "GraphNode", "GraphResult", "KnowledgeGraphBuilder"]

"""Retrieval layer (§15, §16).

Three separated concerns, in order:

* :mod:`~app.ai.retrieval.planner` decides *what* to retrieve - intent, strategy,
  scope and filters - and executes nothing.
* :mod:`~app.ai.retrieval.engine` executes that plan and returns ranked evidence,
  deciding nothing.
* :mod:`~app.ai.retrieval.context` packs the evidence into a Context Package: the
  only thing the RAG engine is allowed to see, which is what makes "answer only from
  supplied evidence" enforceable.

Every query is bounded by the caller's accessible projects. Cross-project retrieval
is prohibited (§1.1), and an unscoped plan returns nothing rather than everything.
"""

from app.ai.retrieval.analysis import QueryAnalysis, QueryAnalysisService
from app.ai.retrieval.context import Citation, ContextAssembler, ContextPackage
from app.ai.retrieval.engine import Evidence, RetrievalEngine, RetrievalResult
from app.ai.retrieval.planner import (
    LevelBudget,
    MetadataFilter,
    RetrievalPlan,
    RetrievalPlanner,
)
from app.ai.retrieval.rerank import IReranker, LLMReranker, NoopReranker, get_reranker

__all__ = [
    "Citation",
    "ContextAssembler",
    "ContextPackage",
    "Evidence",
    "IReranker",
    "LLMReranker",
    "LevelBudget",
    "MetadataFilter",
    "NoopReranker",
    "QueryAnalysis",
    "QueryAnalysisService",
    "RetrievalEngine",
    "RetrievalPlan",
    "RetrievalPlanner",
    "RetrievalResult",
    "get_reranker",
]

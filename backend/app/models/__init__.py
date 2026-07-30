"""SQLAlchemy models.

Importing this package registers every mapper on
:class:`~app.db.base.Base.metadata`. Alembic autogenerate and the test fixtures
both rely on that, so **every new model module must be imported here** - a model
that is not imported is a table Alembic will silently propose to drop.
"""

from app.models.alert import Alert, AlertRule
from app.models.audit import AuditLog, ClauseHistory, ContractHistory, RetrievalAudit
from app.models.chat import ChatMessage, ChatSession
from app.models.chunk import Chunk
from app.models.clause_master import AISettings, ClauseMasterCategory, ClauseMasterRule
from app.models.contract import Contract, ContractMetadata, ContractVersion
from app.models.embedding import Embedding
from app.models.export import ExportJob
from app.models.graph import GraphEdge, GraphNode
from app.models.identity import RefreshToken, Role, User
from app.models.knowledge import (
    Clause,
    ContractSummary,
    Entity,
    KeyDate,
    KnowledgeRelationship,
    Obligation,
    Risk,
)
from app.models.processing import DocumentArtifact, JobStageRun, ProcessingJob
from app.models.profile import DocumentProfile
from app.models.project import Project, ProjectActivity, ProjectMember

__all__ = [
    "AISettings",
    "Alert",
    "AlertRule",
    "AuditLog",
    "ChatMessage",
    "ChatSession",
    "Chunk",
    "Clause",
    "ClauseHistory",
    "ClauseMasterCategory",
    "ClauseMasterRule",
    "Contract",
    "ContractHistory",
    "ContractMetadata",
    "ContractSummary",
    "ContractVersion",
    "DocumentArtifact",
    "DocumentProfile",
    "Embedding",
    "Entity",
    "ExportJob",
    "GraphEdge",
    "GraphNode",
    "JobStageRun",
    "KeyDate",
    "KnowledgeRelationship",
    "Obligation",
    "ProcessingJob",
    "Project",
    "ProjectActivity",
    "ProjectMember",
    "RefreshToken",
    "RetrievalAudit",
    "Risk",
    "Role",
    "User",
]

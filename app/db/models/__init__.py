"""ORM models. Importing this package registers metadata for Alembic."""

from app.db.models.audit import AuditEvent
from app.db.models.collector import (
    CollectionTask,
    CollectorDomainLimit,
    SourceSnapshot,
    WebSource,
)
from app.db.models.document import Document, DocumentChunk, DocumentVersion
from app.db.models.events import DomainEvent, EventConsumerFailure, ProcessedEvent
from app.db.models.membership import (
    OrganizationMembership,
    ProjectMembership,
    WorkspaceMembership,
)
from app.db.models.organization import Organization
from app.db.models.project import Project
from app.db.models.reporting import (
    Report,
    ReportExportJob,
    ReportVersion,
    ResearchCitation,
    ResearchClaim,
    ResearchEvidence,
    ResearchReview,
    ResearchSource,
)
from app.db.models.research import (
    ResearchCheckpoint,
    ResearchFinding,
    ResearchRun,
    ResearchToolCall,
)
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.db.models.workspace import Workspace

__all__ = [
    "AuditEvent",
    "CollectionTask",
    "CollectorDomainLimit",
    "Document",
    "DocumentChunk",
    "DocumentVersion",
    "DomainEvent",
    "EventConsumerFailure",
    "Organization",
    "OrganizationMembership",
    "ProcessedEvent",
    "Project",
    "ProjectMembership",
    "Report",
    "ReportExportJob",
    "ReportVersion",
    "ResearchCheckpoint",
    "ResearchCitation",
    "ResearchClaim",
    "ResearchEvidence",
    "ResearchFinding",
    "ResearchReview",
    "ResearchRun",
    "ResearchSource",
    "ResearchToolCall",
    "SourceSnapshot",
    "Tenant",
    "User",
    "WebSource",
    "Workspace",
    "WorkspaceMembership",
]

"""ORM models. Importing this package registers metadata for Alembic."""

from app.db.models.audit import AuditEvent
from app.db.models.collaboration import ApprovalRequest, ProjectComment
from app.db.models.collector import (
    CollectionTask,
    CollectorDomainLimit,
    SourceSnapshot,
    WebSource,
)
from app.db.models.document import Document, DocumentChunk, DocumentVersion
from app.db.models.events import DomainEvent, EventConsumerFailure, ProcessedEvent
from app.db.models.governance import TenantQuota
from app.db.models.membership import (
    OrganizationMembership,
    ProjectMembership,
    WorkspaceMembership,
)
from app.db.models.monitoring import (
    MonitorAlertRule,
    MonitorChange,
    MonitorRun,
    MonitorSeenSource,
    MonitorSubscription,
    NotificationChannel,
    NotificationDelivery,
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
    "ApprovalRequest",
    "AuditEvent",
    "CollectionTask",
    "CollectorDomainLimit",
    "Document",
    "DocumentChunk",
    "DocumentVersion",
    "DomainEvent",
    "EventConsumerFailure",
    "MonitorAlertRule",
    "MonitorChange",
    "MonitorRun",
    "MonitorSeenSource",
    "MonitorSubscription",
    "NotificationChannel",
    "NotificationDelivery",
    "Organization",
    "OrganizationMembership",
    "ProcessedEvent",
    "Project",
    "ProjectComment",
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
    "TenantQuota",
    "User",
    "WebSource",
    "Workspace",
    "WorkspaceMembership",
]

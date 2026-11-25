"""调研运行、Agent、工具与发现结论的领域枚举。"""

from __future__ import annotations

from enum import StrEnum

RESEARCH_GRAPH_VERSION = "stage5-v1"


class ResearchRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELED = "canceled"
    BUDGET_EXHAUSTED = "budget_exhausted"


TERMINAL_RUN_STATUSES = frozenset(
    {
        ResearchRunStatus.COMPLETED,
        ResearchRunStatus.PARTIAL,
        ResearchRunStatus.FAILED,
        ResearchRunStatus.CANCELED,
        ResearchRunStatus.BUDGET_EXHAUSTED,
    }
)


class ResearchRunKind(StrEnum):
    PRIMARY = "primary"
    FOLLOWUP = "followup"


class ResearchPhase(StrEnum):
    INITIALIZED = "initialized"
    PLANNING = "planning"
    DISPATCHING = "dispatching"
    RESEARCHING = "researching"
    AGGREGATING = "aggregating"
    DECIDING = "deciding"
    REPLANNING = "replanning"
    FINALIZING = "finalizing"
    FINALIZED = "finalized"
    PAUSED = "paused"
    CANCELED = "canceled"
    FAILED = "failed"


class AgentRole(StrEnum):
    SUPERVISOR = "supervisor"
    INDUSTRY = "industry"
    COMPETITOR = "competitor"
    USER = "user"


SPECIALIST_ROLES: tuple[AgentRole, ...] = (
    AgentRole.INDUSTRY,
    AgentRole.COMPETITOR,
    AgentRole.USER,
)


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FindingSourceKind(StrEnum):
    DOCUMENT = "document"
    EXTERNAL_SOURCE = "external_source"


class ResearchToolName(StrEnum):
    KNOWLEDGE_SEARCH = "knowledge_search"
    SOURCE_SEARCH = "source_search"
    SOURCE_READ = "source_read"
    WEB_SEARCH = "web_search"


class StopReason(StrEnum):
    COMPLETED = "completed"
    PARTIAL_FAILURE = "partial_failure"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TIME_BUDGET = "time_budget"
    CANCELED = "canceled"
    PAUSED = "paused"
    NO_FINDINGS = "no_findings"
    AUTHORIZATION_REVOKED = "authorization_revoked"
    PROVIDER_ERROR = "provider_error"
    TASK_FAILURES = "task_failures"


class ToolCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"

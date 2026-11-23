"""Domain enums shared by models, schemas, and policies."""

from __future__ import annotations

from enum import StrEnum


class TenantRole(StrEnum):
    TENANT_ADMIN = "tenant_admin"
    MEMBER = "member"


class ScopeRole(StrEnum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class MembershipStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class WorkspaceStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ProjectClassification(StrEnum):
    STANDARD = "standard"
    CONFIDENTIAL = "confidential"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"


class ResourceType(StrEnum):
    TENANT = "tenant"
    USER = "user"
    ORGANIZATION = "organization"
    WORKSPACE = "workspace"
    PROJECT = "project"
    MEMBERSHIP = "membership"
    DOCUMENT = "document"
    DOCUMENT_VERSION = "document_version"
    COLLECTION_TASK = "collection_task"
    SOURCE = "source"
    SOURCE_SNAPSHOT = "source_snapshot"
    RESEARCH_RUN = "research_run"
    RESEARCH_CHECKPOINT = "research_checkpoint"
    RESEARCH_FINDING = "research_finding"


class Permission(StrEnum):
    ORGANIZATION_CREATE = "organization:create"
    ORGANIZATION_READ = "organization:read"
    ORGANIZATION_UPDATE = "organization:update"
    ORGANIZATION_MANAGE_MEMBERS = "organization:manage_members"

    WORKSPACE_CREATE = "workspace:create"
    WORKSPACE_READ = "workspace:read"
    WORKSPACE_CONTENT_READ = "workspace:content_read"
    WORKSPACE_UPDATE = "workspace:update"
    WORKSPACE_MANAGE_STATUS = "workspace:manage_status"
    WORKSPACE_MANAGE_MEMBERS = "workspace:manage_members"

    PROJECT_CREATE = "project:create"
    PROJECT_READ = "project:read"
    PROJECT_UPDATE = "project:update"
    PROJECT_MANAGE_CLASSIFICATION = "project:manage_classification"
    PROJECT_MANAGE_STATUS = "project:manage_status"
    PROJECT_MANAGE_MEMBERS = "project:manage_members"

    RESEARCH_RUN_CREATE = "research_run:create"
    RESEARCH_RUN_READ = "research_run:read"
    RESEARCH_RUN_CONTROL = "research_run:control"

    USER_READ = "user:read"
    USER_MANAGE = "user:manage"
    AUDIT_READ = "audit:read"


class AuditAction(StrEnum):
    TENANT_CREATED = "tenant.created"
    USER_PROVISIONED = "user.provisioned"
    ORGANIZATION_CREATED = "organization.created"
    ORGANIZATION_UPDATED = "organization.updated"
    WORKSPACE_CREATED = "workspace.created"
    WORKSPACE_UPDATED = "workspace.updated"
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    MEMBERSHIP_GRANTED = "membership.granted"
    MEMBERSHIP_REVOKED = "membership.revoked"
    DOCUMENT_UPLOADED = "document.uploaded"
    DOCUMENT_INDEXED = "document.indexed"
    DOCUMENT_INDEX_FAILED = "document.index_failed"
    DOCUMENT_DELETED = "document.deleted"
    DOCUMENT_DELETE_FAILED = "document.delete_failed"
    COLLECTION_TASK_QUEUED = "collection.task_queued"
    COLLECTION_TASK_SUCCEEDED = "collection.task_succeeded"
    COLLECTION_TASK_FAILED = "collection.task_failed"
    COLLECTION_TASK_RETRIED = "collection.task_retried"
    SOURCE_CREATED = "source.created"
    SOURCE_UPDATED = "source.updated"
    SOURCE_DELETED = "source.deleted"
    SOURCE_DELETE_FAILED = "source.delete_failed"
    RESEARCH_RUN_CREATED = "research_run.created"
    RESEARCH_RUN_PAUSED = "research_run.paused"
    RESEARCH_RUN_RESUMED = "research_run.resumed"
    RESEARCH_RUN_CANCELED = "research_run.canceled"
    RESEARCH_RUN_COMPLETED = "research_run.completed"
    RESEARCH_RUN_FAILED = "research_run.failed"
    RESEARCH_RUN_BUDGET_EXHAUSTED = "research_run.budget_exhausted"
    AUTHORIZATION_DENIED = "authorization.denied"

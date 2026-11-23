"""Tenancy API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.auth.enums import (
    MembershipStatus,
    OrganizationStatus,
    ProjectClassification,
    ProjectStatus,
    ScopeRole,
    TenantStatus,
    WorkspaceStatus,
)

Slug = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$",
        min_length=2,
        max_length=64,
    ),
]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class TenantRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    slug: str
    name: str
    status: TenantStatus


class OrganizationCreate(BaseModel):
    slug: Slug
    name: DisplayName


class OrganizationUpdate(BaseModel):
    name: DisplayName | None = None
    status: OrganizationStatus | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> OrganizationUpdate:
        if self.name is None and self.status is None:
            raise ValueError("At least one field must be provided")
        return self


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    slug: str
    name: str
    status: OrganizationStatus
    created_at: datetime
    updated_at: datetime


class WorkspaceCreate(BaseModel):
    slug: Slug
    name: DisplayName


class WorkspaceUpdate(BaseModel):
    name: DisplayName | None = None
    status: WorkspaceStatus | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> WorkspaceUpdate:
        if self.name is None and self.status is None:
            raise ValueError("At least one field must be provided")
        return self


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    organization_id: UUID
    slug: str
    name: str
    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime


class ProjectCreate(BaseModel):
    slug: Slug
    name: DisplayName
    classification: ProjectClassification = ProjectClassification.STANDARD


class ProjectUpdate(BaseModel):
    name: DisplayName | None = None
    status: ProjectStatus | None = None
    classification: ProjectClassification | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> ProjectUpdate:
        if self.name is None and self.status is None and self.classification is None:
            raise ValueError("At least one field must be provided")
        return self


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    workspace_id: UUID
    slug: str
    name: str
    status: ProjectStatus
    classification: ProjectClassification
    created_at: datetime
    updated_at: datetime


class MembershipGrant(BaseModel):
    user_id: UUID
    role: ScopeRole


class MembershipRead(BaseModel):
    id: UUID
    user_id: UUID
    email: str
    display_name: str
    role: ScopeRole
    status: MembershipStatus
    created_at: datetime
    updated_at: datetime


class MembershipView(BaseModel):
    scope_type: Literal["organization", "workspace", "project"]
    scope_id: UUID
    role: ScopeRole


class Pagination(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)

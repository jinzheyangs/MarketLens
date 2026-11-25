"""用户与主体 Schema。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, StringConstraints, field_validator

from app.auth.enums import TenantRole, UserStatus
from app.schemas.tenancy import MembershipView, TenantRead

Subject = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]
DisplayName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]


class UserProvisionRequest(BaseModel):
    subject: Subject
    email: EmailStr
    display_name: DisplayName
    tenant_role: TenantRole = TenantRole.MEMBER

    @field_validator("email", mode="before")
    @classmethod
    def _normalize_email(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    display_name: str
    tenant_role: TenantRole
    status: UserStatus
    created_at: datetime
    updated_at: datetime


class MeResponse(BaseModel):
    user: UserRead
    tenant: TenantRead
    memberships: list[MembershipView]

"""按租户配置采集域名放行/拦截策略。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError
from app.db.models import TenantDomainPolicy
from app.db.tenant_scope import tenant_scoped_select

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_MAX_DOMAINS = 500


@dataclass(frozen=True, slots=True)
class DomainPolicy:
    allowed_domains: tuple[str, ...] = ()
    blocked_domains: tuple[str, ...] = ()

    def allows(self, host: str) -> bool:
        normalized = host.strip().lower().rstrip(".")
        if any(_matches(normalized, domain) for domain in self.blocked_domains):
            return False
        if not self.allowed_domains:
            return True
        return any(_matches(normalized, domain) for domain in self.allowed_domains)


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def normalize_domains(values: list[str]) -> list[str]:
    if len(values) > _MAX_DOMAINS:
        raise BadRequestError(f"at most {_MAX_DOMAINS} domains are allowed")
    normalized: list[str] = []
    for value in values:
        candidate = value.strip().lower().lstrip("*.").rstrip(".")
        if not candidate or len(candidate) > 253 or "/" in candidate or "\\" in candidate:
            raise BadRequestError("domain policy entries must be host suffixes")
        if not all(_LABEL.fullmatch(label) for label in candidate.split(".")):
            raise BadRequestError("domain policy entries must be host suffixes")
        if candidate not in normalized:
            normalized.append(candidate)
    return normalized


async def get_domain_policy(
    session: AsyncSession,
    *,
    tenant_id: UUID,
) -> DomainPolicy:
    row = await session.scalar(
        tenant_scoped_select(TenantDomainPolicy).where(TenantDomainPolicy.tenant_id == tenant_id)
    )
    if row is None:
        return DomainPolicy()
    return DomainPolicy(
        allowed_domains=tuple(str(value) for value in row.allowed_domains),
        blocked_domains=tuple(str(value) for value in row.blocked_domains),
    )


async def upsert_domain_policy(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    allowed_domains: list[str] | None,
    blocked_domains: list[str] | None,
    updated_by: UUID,
) -> tuple[TenantDomainPolicy, DomainPolicy]:
    normalized_allowed = normalize_domains(allowed_domains) if allowed_domains is not None else None
    normalized_blocked = normalize_domains(blocked_domains) if blocked_domains is not None else None
    row = await session.scalar(
        tenant_scoped_select(TenantDomainPolicy).where(TenantDomainPolicy.tenant_id == tenant_id)
    )
    if row is None:
        row = TenantDomainPolicy(
            id=uuid4(),
            tenant_id=tenant_id,
            allowed_domains=normalized_allowed or [],
            blocked_domains=normalized_blocked or [],
            updated_by=updated_by,
        )
        session.add(row)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            row = await session.scalar(
                tenant_scoped_select(TenantDomainPolicy).where(
                    TenantDomainPolicy.tenant_id == tenant_id
                )
            )
            if row is None:
                raise
    if normalized_allowed is not None:
        row.allowed_domains = normalized_allowed
    if normalized_blocked is not None:
        row.blocked_domains = normalized_blocked
    row.updated_by = updated_by
    await session.flush()
    await session.refresh(row)
    return row, DomainPolicy(
        allowed_domains=tuple(str(value) for value in row.allowed_domains),
        blocked_domains=tuple(str(value) for value in row.blocked_domains),
    )


__all__ = [
    "DomainPolicy",
    "get_domain_policy",
    "normalize_domains",
    "upsert_domain_policy",
]

"""PostgreSQL-backed per-tenant/domain start-rate limiter.

The atomic upsert reserves a slot and returns the time at which the caller may
proceed. It works across worker processes without relying on an in-memory cache.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_RESERVE_SLOT = text(
    """
    INSERT INTO collector_domain_limits
        (id, tenant_id, domain, next_allowed_at)
    VALUES
        (:id, :tenant_id, :domain, now() + make_interval(secs => :interval))
    ON CONFLICT (tenant_id, domain)
    DO UPDATE SET
        next_allowed_at = GREATEST(
            collector_domain_limits.next_allowed_at,
            now()
        ) + make_interval(secs => :interval),
        updated_at = now()
    RETURNING next_allowed_at - make_interval(secs => :interval) AS slot
    """
)


class DomainRateLimiter:
    async def reserve(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        domain: str,
        interval_seconds: float,
    ) -> datetime:
        interval = max(0.0, interval_seconds)
        row = (
            await session.execute(
                _RESERVE_SLOT,
                {
                    "id": uuid4(),
                    "tenant_id": tenant_id,
                    "domain": domain[:255],
                    "interval": interval,
                },
            )
        ).one()
        slot = row[0]
        if not isinstance(slot, datetime):
            raise RuntimeError("rate limiter returned an invalid slot")
        if slot.tzinfo is None:
            slot = slot.replace(tzinfo=UTC)
        return slot

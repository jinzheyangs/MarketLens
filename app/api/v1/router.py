"""Versioned API router.

Authentication and principal loading are applied at the router level so new
business routes are protected by default. Authorization is enforced per action
inside each endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Security

from app.api.v1 import (
    audit,
    collaboration,
    collector,
    documents,
    events,
    me,
    monitoring,
    operations,
    organizations,
    projects,
    quotas,
    reports,
    research,
    usage,
    users,
    workspaces,
)
from app.auth.dependencies import bearer_scheme, get_principal

router = APIRouter(
    dependencies=[Security(bearer_scheme), Depends(get_principal)],
)

router.include_router(me.router)
router.include_router(users.router)
router.include_router(organizations.router)
router.include_router(workspaces.router)
router.include_router(projects.router)
router.include_router(documents.router)
router.include_router(collector.router)
router.include_router(research.router)
router.include_router(reports.router)
router.include_router(monitoring.router)
router.include_router(collaboration.router)
router.include_router(quotas.router)
router.include_router(usage.router)
router.include_router(operations.router)
router.include_router(events.router)
router.include_router(audit.router)

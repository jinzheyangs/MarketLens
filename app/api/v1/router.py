"""带版本前缀的 API 总路由。

鉴权与主体加载在路由级别统一应用，新增业务路由默认受保护；具体授权
由各接口内部按动作执行校验。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Security

from app.api.v1 import (
    audit,
    collaboration,
    collector,
    documents,
    domain_policies,
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

# 总路由：统一挂载 Bearer 认证与主体解析依赖
router = APIRouter(
    dependencies=[Security(bearer_scheme), Depends(get_principal)],
)

# 逐域挂载子路由
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
router.include_router(domain_policies.router)
router.include_router(events.router)
router.include_router(audit.router)

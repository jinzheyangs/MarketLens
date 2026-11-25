"""供编排平台调用的未鉴权存活/就绪探针。"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.schemas.health import LivenessResponse, ReadinessResponse
from app.services.health import HealthService

router = APIRouter(prefix="/health", tags=["health"])


def _service(request: Request) -> HealthService:
    """取用应用挂载的健康检查服务。"""
    service = getattr(request.app.state, "health_service", None)
    if service is None:
        raise RuntimeError("Health service is not initialized")
    return cast(HealthService, service)


@router.get(
    "/live",
    response_model=LivenessResponse,
    summary="Liveness probe",
)
async def liveness(request: Request) -> LivenessResponse:
    """存活探针：进程是否在运行。"""
    return await _service(request).liveness()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={
        503: {
            "model": ReadinessResponse,
            "description": "One or more required dependencies are unavailable.",
        }
    },
)
async def readiness(request: Request) -> Response:
    """就绪探针：必需依赖是否可用；不可用时返回 503。"""
    report = await _service(request).readiness()
    status_code = 200 if report.status == "ok" else 503
    return JSONResponse(
        status_code=status_code,
        content=report.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )

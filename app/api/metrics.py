"""Prometheus 指标抓取端点。

该端点挂载在 OIDC 保护的 API 前缀之外。配置 ``METRICS_BEARER_TOKEN`` 后，
抓取方需携带 ``Authorization: Bearer <token>``；生产环境要求必须配置令牌。
"""

from __future__ import annotations

import hmac
from typing import cast

from fastapi import APIRouter, Request, Response

from app.core.config import Settings
from app.core.metrics import Metrics


def build_metrics_router(settings: Settings) -> APIRouter:
    """构建指标路由（按配置路径暴露抓取端点）。"""
    router = APIRouter(tags=["metrics"])

    @router.get(settings.metrics_path, include_in_schema=False)
    async def scrape(request: Request) -> Response:
        # 可选的 Bearer 令牌校验（常量时间比较，防时序侧信道）
        expected = settings.metrics_bearer_token
        if expected is not None:
            authorization = request.headers.get("authorization", "")
            scheme, _, supplied = authorization.partition(" ")
            if scheme.lower() != "bearer" or not supplied:
                return Response(status_code=401)
            if not hmac.compare_digest(
                supplied.encode("utf-8"),
                expected.get_secret_value().encode("utf-8"),
            ):
                return Response(status_code=401)
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is None:
            return Response(status_code=404)
        typed = cast(Metrics, metrics)
        return Response(
            content=typed.render(),
            media_type=typed.content_type,
            headers={"Cache-Control": "no-store"},
        )

    return router

"""Prometheus scrape endpoint.

The endpoint is mounted outside the OIDC-protected API prefix. When
``METRICS_BEARER_TOKEN`` is configured, scrapers must present
``Authorization: Bearer <token>``. Production validation requires a token.
"""

from __future__ import annotations

import hmac
from typing import cast

from fastapi import APIRouter, Request, Response

from app.core.config import Settings
from app.core.metrics import Metrics


def build_metrics_router(settings: Settings) -> APIRouter:
    router = APIRouter(tags=["metrics"])

    @router.get(settings.metrics_path, include_in_schema=False)
    async def scrape(request: Request) -> Response:
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

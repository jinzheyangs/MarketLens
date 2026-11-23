"""Unified JSON error envelope used by handlers and ASGI middleware."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi.responses import JSONResponse

from app.core.context import current_request_id
from app.core.errors import ErrorCode


def error_payload(
    code: ErrorCode,
    message: str,
    *,
    details: Mapping[str, Any] | list[Any] | None = None,
    retryable: bool = False,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "code": str(code),
        "message": message,
        "request_id": current_request_id(),
        "retryable": retryable,
    }
    if details is not None:
        error["details"] = details
    return {"error": error}


def error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    *,
    details: Mapping[str, Any] | list[Any] | None = None,
    retryable: bool = False,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message, details=details, retryable=retryable),
        headers=dict(headers) if headers else None,
    )

"""统一的 JSON 错误响应信封。

供异常处理器与 ASGI 中间件复用，保证所有错误响应结构一致：
``{"error": {"code", "message", "request_id", "retryable", "details?"}}``。
"""

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
    """构造错误响应体字典，自动带上当前请求 ID。"""
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
    """构造带统一信封与可选响应头的 JSON 错误响应。"""
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message, details=details, retryable=retryable),
        headers=dict(headers) if headers else None,
    )

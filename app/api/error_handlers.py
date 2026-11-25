"""统一的异常到 JSON 响应转换。

所有响应共用 ``{"error": {...}}`` 信封并带请求 ID。校验细节只包含字段
位置/类型，绝不回显提交的原始输入。
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger
from app.core.responses import error_response

logger = get_logger(__name__)

# HTTP 状态码 -> 错误码与默认提示的映射
_HTTP_ERROR_CODES: dict[int, tuple[ErrorCode, str]] = {
    400: (ErrorCode.BAD_REQUEST, "The request is invalid."),
    401: (ErrorCode.UNAUTHORIZED, "Authentication is required."),
    403: (ErrorCode.FORBIDDEN, "The operation is not permitted."),
    404: (ErrorCode.NOT_FOUND, "The requested resource was not found."),
    405: (ErrorCode.METHOD_NOT_ALLOWED, "The HTTP method is not allowed."),
    409: (ErrorCode.CONFLICT, "The request conflicts with the current state."),
    413: (ErrorCode.PAYLOAD_TOO_LARGE, "The request body exceeds the permitted size."),
    429: (ErrorCode.RATE_LIMITED, "Too many requests."),
    503: (ErrorCode.SERVICE_UNAVAILABLE, "The service is temporarily unavailable."),
}


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理可预期的应用异常 ``AppError``。"""
    if not isinstance(exc, AppError):  # pragma: no cover - defensive
        return await _unhandled_exception_handler(request, exc)

    # 5xx 级别错误记为错误日志
    if exc.status_code >= 500:
        logger.error(
            "application_error",
            extra={
                "context": {
                    "code": str(exc.code),
                    "status": exc.status_code,
                    "method": request.method,
                    "path": request.url.path,
                }
            },
        )
    return error_response(
        exc.status_code,
        exc.code,
        exc.message,
        details=exc.details,
        retryable=exc.retryable,
        headers=exc.headers,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理请求参数校验错误，汇总字段位置与错误类型。"""
    if not isinstance(exc, RequestValidationError):  # pragma: no cover - defensive
        return await _unhandled_exception_handler(request, exc)

    details: list[dict[str, Any]] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        details.append(
            {
                "field": location or "body",
                "message": str(error.get("msg", "Invalid value.")),
                "type": str(error.get("type", "value_error")),
            }
        )
    logger.info(
        "request_validation_failed",
        extra={
            "context": {
                "method": request.method,
                "path": request.url.path,
                "error_count": len(details),
            }
        },
    )
    return error_response(
        422,
        ErrorCode.VALIDATION_ERROR,
        "Request validation failed.",
        details=details,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """处理 Starlette 内置 HTTP 异常，映射到统一错误码。"""
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover - defensive
        return await _unhandled_exception_handler(request, exc)

    code, message = _HTTP_ERROR_CODES.get(
        exc.status_code,
        (ErrorCode.BAD_REQUEST, "The request could not be processed."),
    )
    try:
        fallback_message = HTTPStatus(exc.status_code).phrase
    except ValueError:
        fallback_message = message
    if exc.status_code not in _HTTP_ERROR_CODES:
        message = fallback_message

    headers = dict(exc.headers) if exc.headers else None
    return error_response(exc.status_code, code, message, headers=headers)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底处理未预期异常：记录堆栈，对外仅返回通用 500 信封。"""
    logger.error(
        "unhandled_exception",
        extra={
            "context": {
                "method": request.method,
                "path": request.url.path,
            }
        },
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return error_response(
        500,
        ErrorCode.INTERNAL_ERROR,
        "An internal error occurred.",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """向应用注册各类异常处理器。"""
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

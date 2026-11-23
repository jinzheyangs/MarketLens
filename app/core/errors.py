"""Stable application error codes and the exception hierarchy.

Messages attached to ``AppError`` are safe to return to clients. Internal
exception details and stack traces are never serialized into responses.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    BAD_REQUEST = "bad_request"
    VALIDATION_ERROR = "validation_error"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    REQUEST_TIMEOUT = "request_timeout"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    CONFLICT = "conflict"
    RATE_LIMITED = "rate_limited"
    TENANT_CONTEXT_REQUIRED = "tenant_context_required"
    TENANT_ISOLATION_VIOLATION = "tenant_isolation_violation"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


class AppError(Exception):
    """Expected, client-safe application error."""

    def __init__(
        self,
        *,
        code: ErrorCode,
        message: str,
        status_code: int = 400,
        details: Mapping[str, Any] | list[Any] | None = None,
        retryable: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details
        self.retryable = retryable
        self.headers = dict(headers) if headers else None


class AuthenticationError(AppError):
    def __init__(
        self,
        message: str = "Authentication is required.",
        *,
        invalid_token: bool = False,
    ) -> None:
        challenge = 'Bearer error="invalid_token"' if invalid_token else "Bearer"
        super().__init__(
            code=ErrorCode.UNAUTHORIZED,
            message=message,
            status_code=401,
            headers={"WWW-Authenticate": challenge},
        )


class ForbiddenError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.FORBIDDEN,
            message="The operation is not permitted.",
            status_code=403,
        )


class ServiceUnavailableError(AppError):
    def __init__(
        self,
        message: str = "The service is temporarily unavailable.",
    ) -> None:
        super().__init__(
            code=ErrorCode.SERVICE_UNAVAILABLE,
            message=message,
            status_code=503,
            retryable=True,
        )


class NotFoundError(AppError):
    def __init__(self, message: str = "The requested resource was not found.") -> None:
        super().__init__(
            code=ErrorCode.NOT_FOUND,
            message=message,
            status_code=404,
        )


class ConflictError(AppError):
    def __init__(self, message: str = "The request conflicts with the current state.") -> None:
        super().__init__(
            code=ErrorCode.CONFLICT,
            message=message,
            status_code=409,
        )


class TenantContextError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.TENANT_CONTEXT_REQUIRED,
            message="A tenant context is required for this operation.",
            status_code=500,
        )


class TenantIsolationError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.TENANT_ISOLATION_VIOLATION,
            message="The requested operation crosses tenant boundaries.",
            status_code=403,
        )


class RequestEntityTooLargeError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message="The request body exceeds the permitted size.",
            status_code=413,
        )


class RequestTimeoutAppError(AppError):
    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.REQUEST_TIMEOUT,
            message="The request exceeded the permitted processing time.",
            status_code=504,
            retryable=True,
        )

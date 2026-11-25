"""稳定的应用错误码与异常体系。

``AppError`` 上携带的消息可以安全返回给客户端；内部异常细节与堆栈
绝不会序列化进响应。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):   #ErrorCode 枚举（StrEnum）
    """业务错误码枚举，与响应体 ``error.code`` 一一对应。"""

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
    QUOTA_EXCEEDED = "quota_exceeded"
    TENANT_CONTEXT_REQUIRED = "tenant_context_required"
    TENANT_ISOLATION_VIOLATION = "tenant_isolation_violation"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INTERNAL_ERROR = "internal_error"


class AppError(Exception):
    """可预期、对客户端安全的应用异常基类。"""

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
        self.code = code  # 机器可读错误码
        self.message = message  # 可安全对外展示的提示
        self.status_code = status_code  # 对应 HTTP 状态码
        self.details = details  # 结构化补充信息（可选）
        self.retryable = retryable  # 调用方是否可安全重试
        self.headers = dict(headers) if headers else None


class AuthenticationError(AppError):
    """未认证或令牌无效，返回 401 并带 WWW-Authenticate 挑战头。"""

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


class BadRequestError(AppError):
    """请求参数/语义不合法，返回 400。"""

    def __init__(self, message: str = "The request is invalid.") -> None:
        super().__init__(
            code=ErrorCode.BAD_REQUEST,
            message=message,
            status_code=400,
        )


class ForbiddenError(AppError):
    """已认证但无权限执行该操作，返回 403。"""

    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.FORBIDDEN,
            message="The operation is not permitted.",
            status_code=403,
        )


class ServiceUnavailableError(AppError):
    """依赖服务暂不可用，返回 503，标记为可重试。"""

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


class QuotaExceededError(AppError):
    """租户配额超限，返回 429，不可自动重试。"""

    def __init__(self, message: str = "The tenant quota has been exceeded.") -> None:
        super().__init__(
            code=ErrorCode.QUOTA_EXCEEDED,
            message=message,
            status_code=429,
            retryable=False,
        )


class NotFoundError(AppError):
    """资源不存在，返回 404。"""

    def __init__(self, message: str = "The requested resource was not found.") -> None:
        super().__init__(
            code=ErrorCode.NOT_FOUND,
            message=message,
            status_code=404,
        )


class ConflictError(AppError):
    """与当前资源状态冲突（如重复创建），返回 409。"""

    def __init__(self, message: str = "The request conflicts with the current state.") -> None:
        super().__init__(
            code=ErrorCode.CONFLICT,
            message=message,
            status_code=409,
        )


class TenantContextError(AppError):
    """缺少必需的租户上下文，属于服务端集成错误，返回 500。"""

    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.TENANT_CONTEXT_REQUIRED,
            message="A tenant context is required for this operation.",
            status_code=500,
        )


class TenantIsolationError(AppError):
    """操作跨越了租户边界，违反隔离约束，返回 403。"""

    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.TENANT_ISOLATION_VIOLATION,
            message="The requested operation crosses tenant boundaries.",
            status_code=403,
        )


class RequestEntityTooLargeError(AppError):
    """请求体超过允许大小，返回 413。"""

    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message="The request body exceeds the permitted size.",
            status_code=413,
        )


class RequestTimeoutAppError(AppError):
    """请求超过允许处理时长，返回 504，标记为可重试。"""

    def __init__(self) -> None:
        super().__init__(
            code=ErrorCode.REQUEST_TIMEOUT,
            message="The request exceeded the permitted processing time.",
            status_code=504,
            retryable=True,
        )

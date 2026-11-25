"""Bearer 令牌鉴权与租户上下文中间件。

API 前缀下的所有路径默认要求已校验令牌。中间件只校验 JWT 并绑定租户
上下文，刻意不读取角色/权限声明；主体与成员关系由请求依赖层从数据库加载。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth.oidc import TokenVerificationError, TokenVerifier
from app.core.context import reset_tenant_id, set_tenant_id
from app.core.errors import ErrorCode
from app.core.responses import error_response

logger = logging.getLogger(__name__)

_MAX_AUTHORIZATION_HEADER_LENGTH = 8300


class AuthenticationMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        api_prefix: str,
        verifier_provider: Callable[[], TokenVerifier | None],
        public_api_paths: Collection[str] = (),
    ) -> None:
        self.app = app
        self.api_prefix = api_prefix.rstrip("/")
        self.verifier_provider = verifier_provider
        self.public_api_paths = frozenset(public_api_paths)

    def _requires_authentication(self, path: str) -> bool:
        if path in self.public_api_paths:
            return False
        return path == self.api_prefix or path.startswith(f"{self.api_prefix}/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._requires_authentication(str(scope.get("path", ""))):
            await self.app(scope, receive, send)
            return

        verifier = self.verifier_provider()
        if verifier is None:
            # 未配置校验器：鉴权未就绪，拒绝请求
            logger.error(
                "authentication_not_configured",
                extra={"context": {"path": scope.get("path")}},
            )
            await self._reject(
                scope,
                receive,
                send,
                message="Authentication is not configured.",
                invalid_token=False,
            )
            return

        authorization = Headers(scope=scope).get("authorization")
        if authorization is None:
            await self._reject(
                scope,
                receive,
                send,
                message="Authentication is required.",
                invalid_token=False,
            )
            return
        if len(authorization) > _MAX_AUTHORIZATION_HEADER_LENGTH:
            await self._reject_token(scope, receive, send, reason="header_too_large")
            return

        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token.strip():
            await self._reject_token(scope, receive, send, reason="invalid_scheme")
            return
        token = token.strip()

        try:
            claims = await verifier.verify(token)
        except TokenVerificationError as exc:
            await self._reject_token(scope, receive, send, reason=exc.reason)
            return

        # 校验通过：把声明与租户 ID 写入请求状态，并绑定当前上下文
        state = scope.setdefault("state", {})
        if isinstance(state, dict):
            state["claims"] = claims
            state["tenant_id"] = str(claims.tenant_id)

        context_token = set_tenant_id(claims.tenant_id)
        try:
            await self.app(scope, receive, send)
        finally:
            # 退出请求时还原租户上下文，避免跨请求串租户
            reset_tenant_id(context_token)

    async def _reject_token(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        reason: str,
    ) -> None:
        logger.warning(
            "token_rejected",
            extra={
                "context": {
                    "reason": reason,
                    "path": scope.get("path"),
                }
            },
        )
        await self._reject(
            scope,
            receive,
            send,
            message="The authentication token is invalid or expired.",
            invalid_token=True,
        )

    async def _reject(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        message: str,
        invalid_token: bool,
    ) -> None:
        challenge = 'Bearer error="invalid_token"' if invalid_token else "Bearer"
        response = error_response(
            401,
            ErrorCode.UNAUTHORIZED,
            message,
            headers={"WWW-Authenticate": challenge},
        )
        await response(scope, receive, send)

"""OIDC/JWT resource-server boundary.

The backend never performs a browser authorization-code flow and never trusts
ID-token claims such as roles or permissions for resource authorization. It only
verifies the issuer signature/audience/expiry and extracts ``(iss, sub)`` plus a
tenant lookup claim. Memberships are loaded from PostgreSQL for every request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import jwt
from jwt import PyJWKClient, PyJWTError

from app.core.config import Settings

_MAX_TOKEN_LENGTH = 8192


class TokenVerificationError(Exception):
    """Raised for any invalid token; ``reason`` is safe for server logs."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TokenClaims:
    issuer: str
    subject: str
    tenant_id: UUID


class TokenVerifier(Protocol):
    async def verify(self, token: str) -> TokenClaims: ...


class JWTVerifier:
    """Verify asymmetric JWTs and extract only the identity fields we need."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        algorithms: Sequence[str],
        key_resolver: Callable[[str], Any],
        tenant_claim: str = "tenant_id",
        leeway_seconds: int = 60,
    ) -> None:
        if not algorithms:
            raise ValueError("At least one JWT algorithm is required")
        self._issuer = issuer
        self._audience = audience
        self._algorithms = tuple(algorithms)
        self._key_resolver = key_resolver
        self._tenant_claim = tenant_claim
        self._leeway_seconds = leeway_seconds

    async def verify(self, token: str) -> TokenClaims:
        return await asyncio.to_thread(self._verify_sync, token)

    def _verify_sync(self, token: str) -> TokenClaims:
        if not token or len(token) > _MAX_TOKEN_LENGTH:
            raise TokenVerificationError("malformed_token")

        try:
            header = jwt.get_unverified_header(token)
        except PyJWTError as exc:
            raise TokenVerificationError("malformed_token") from exc
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in self._algorithms:
            raise TokenVerificationError("algorithm_not_allowed")

        try:
            signing_key = self._key_resolver(token)
        except Exception as exc:  # JWKS transport/parse failures
            raise TokenVerificationError("signing_key_unavailable") from exc

        try:
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except PyJWTError as exc:
            raise TokenVerificationError("invalid_token") from exc

        issuer = claims.get("iss")
        subject = claims.get("sub")
        tenant_claim = claims.get(self._tenant_claim)

        if not isinstance(issuer, str) or not issuer or len(issuer) > 512:
            raise TokenVerificationError("invalid_issuer")
        if not isinstance(subject, str) or not subject or len(subject) > 255:
            raise TokenVerificationError("invalid_subject")
        if not isinstance(tenant_claim, str) or not tenant_claim:
            raise TokenVerificationError("missing_tenant_claim")
        try:
            tenant_id = UUID(tenant_claim)
        except ValueError as exc:
            raise TokenVerificationError("invalid_tenant_claim") from exc

        return TokenClaims(issuer=issuer, subject=subject, tenant_id=tenant_id)


def build_token_verifier(settings: Settings) -> TokenVerifier | None:
    """Build the configured verifier; ``None`` means authentication is disabled."""
    if not settings.oidc_enabled:
        return None
    if not settings.oidc_issuer or not settings.oidc_audience or not settings.oidc_jwks_uri:
        raise RuntimeError("OIDC is enabled but issuer/audience/JWKS URI is missing")

    jwks_client = PyJWKClient(
        settings.oidc_jwks_uri,
        cache_jwk_set=True,
        lifespan=settings.oidc_jwks_cache_seconds,
        timeout=settings.oidc_http_timeout_seconds,
    )

    def resolve_signing_key(token: str) -> Any:
        return jwks_client.get_signing_key_from_jwt(token).key

    return JWTVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        algorithms=tuple(settings.oidc_algorithms),
        key_resolver=resolve_signing_key,
        tenant_claim=settings.oidc_tenant_claim,
        leeway_seconds=settings.oidc_leeway_seconds,
    )

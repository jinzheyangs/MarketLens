from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth.oidc import JWTVerifier, TokenVerificationError

ISSUER = "https://idp.example.test"
AUDIENCE = "marketlens-api"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()
_OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _claims(**overrides: object) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "tenant_id": str(uuid4()),
        "iat": now,
        "exp": now + 120,
    }
    claims.update(overrides)
    return claims


def _verifier(
    *,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    key_resolver=None,
) -> JWTVerifier:
    return JWTVerifier(
        issuer=issuer,
        audience=audience,
        algorithms=("RS256",),
        key_resolver=key_resolver or (lambda _token: _PUBLIC_KEY),
        leeway_seconds=0,
    )


def _encode(claims: dict[str, object], key=_PRIVATE_KEY) -> str:
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


async def test_valid_token_is_mapped_to_identity_fields() -> None:
    tenant_id = uuid4()
    token = _encode(
        _claims(
            tenant_id=str(tenant_id),
            role="tenant_admin",
            permissions=["*"],
        )
    )
    claims = await _verifier().verify(token)
    assert claims.issuer == ISSUER
    assert claims.subject == "user-1"
    assert claims.tenant_id == tenant_id
    assert not hasattr(claims, "role")
    assert not hasattr(claims, "permissions")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"exp": int(time.time()) - 60}, "invalid_token"),
        ({"iss": "https://other.example.test"}, "invalid_token"),
        ({"aud": "other-audience"}, "invalid_token"),
        ({"tenant_id": None}, "missing_tenant_claim"),
        ({"tenant_id": "not-a-uuid"}, "invalid_tenant_claim"),
        ({"sub": ""}, "invalid_subject"),
        ({"iat": None}, "invalid_token"),
    ],
)
async def test_invalid_claims_are_rejected(
    overrides: dict[str, object],
    reason: str,
) -> None:
    with pytest.raises(TokenVerificationError) as exc_info:
        await _verifier().verify(_encode(_claims(**overrides)))
    assert exc_info.value.reason == reason


async def test_bad_signature_is_rejected() -> None:
    with pytest.raises(TokenVerificationError) as exc_info:
        await _verifier().verify(_encode(_claims(), key=_OTHER_PRIVATE_KEY))
    assert exc_info.value.reason == "invalid_token"


async def test_symmetric_algorithm_is_rejected() -> None:
    token = jwt.encode(_claims(), "shared-secret-that-is-long-enough-for-hs256", algorithm="HS256")
    with pytest.raises(TokenVerificationError) as exc_info:
        await _verifier().verify(token)
    assert exc_info.value.reason == "algorithm_not_allowed"


async def test_malformed_and_oversized_tokens_are_rejected() -> None:
    with pytest.raises(TokenVerificationError) as malformed:
        await _verifier().verify("not-a-jwt")
    assert malformed.value.reason == "malformed_token"

    with pytest.raises(TokenVerificationError) as oversized:
        await _verifier().verify("a" * 9000)
    assert oversized.value.reason == "malformed_token"


async def test_signing_key_failure_is_reported_without_details() -> None:
    def failing_resolver(_token: str) -> object:
        raise RuntimeError("jwks endpoint contains a secret")

    with pytest.raises(TokenVerificationError) as exc_info:
        await _verifier(key_resolver=failing_resolver).verify(_encode(_claims()))
    assert exc_info.value.reason == "signing_key_unavailable"
    assert "secret" not in str(exc_info.value)

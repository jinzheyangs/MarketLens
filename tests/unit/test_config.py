from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import (
    DEFAULT_TEST_DATABASE_URL,
    make_production_settings,
    make_settings,
)


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_database_url_requires_asyncpg_driver() -> None:
    with pytest.raises(ValidationError):
        make_settings(database_url="postgresql://market_intel:secret@127.0.0.1:5432/market_intel")


def test_secrets_do_not_appear_in_repr() -> None:
    settings = make_settings(
        database_url="postgresql+asyncpg://market_intel:super-secret@127.0.0.1:5432/db",
        qdrant_url="http://127.0.0.1:6333",
        qdrant_api_key="qdrant-secret",
    )
    rendered = repr(settings)
    assert "super-secret" not in rendered
    assert "qdrant-secret" not in rendered
    assert settings.safe_database_url == "postgresql+asyncpg://127.0.0.1:5432/db"
    assert "super-secret" not in settings.safe_database_url


def test_comma_separated_and_json_lists() -> None:
    settings = make_settings(
        allowed_hosts="app.example.com, admin.example.com",
        cors_allowed_origins='["https://frontend.example.com/", "https://other.example.com/"]',
    )
    assert settings.allowed_hosts == ["app.example.com", "admin.example.com"]
    assert settings.cors_allowed_origins == [
        "https://frontend.example.com",
        "https://other.example.com",
    ]


def test_dotenv_style_comma_separated_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: complex fields must not be JSON-decoded at the source layer."""
    from app.core.config import Settings

    monkeypatch.setenv("ALLOWED_HOSTS", "app.example.com,admin.example.com")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://frontend.example.com/,")
    settings = Settings(_env_file=None)
    assert settings.allowed_hosts == ["app.example.com", "admin.example.com"]
    assert settings.cors_allowed_origins == ["https://frontend.example.com"]


def test_empty_qdrant_key_is_treated_as_unset() -> None:
    settings = make_settings(qdrant_api_key="", qdrant_url="")
    assert settings.qdrant_api_key is None
    with pytest.raises(ValidationError):
        make_settings(qdrant_api_key="secret")


def test_enum_settings_are_case_insensitive() -> None:
    settings = make_settings(environment="TEST", log_level="debug", log_format="JSON")
    assert settings.environment == "test"
    assert settings.log_level == "DEBUG"
    assert settings.log_format == "json"


def test_request_id_header_is_validated() -> None:
    with pytest.raises(ValidationError):
        make_settings(request_id_header="Bad Header")
    assert make_settings(request_id_header="X-Correlation-ID").request_id_header == (
        "X-Correlation-ID"
    )


def test_service_urls_reject_embedded_credentials() -> None:
    with pytest.raises(ValidationError):
        make_settings(qdrant_url="http://user:password@qdrant:6333")
    with pytest.raises(ValidationError):
        make_settings(
            otel_enabled=True,
            otel_exporter_otlp_endpoint="http://user:password@collector:4318/v1/traces",
        )


def test_otel_requires_endpoint() -> None:
    with pytest.raises(ValidationError):
        make_settings(otel_enabled=True)


def test_production_requires_explicit_allowed_hosts() -> None:
    with pytest.raises(ValidationError):
        make_production_settings(allowed_hosts=[])

    development = make_settings(environment="development", allowed_hosts=[])
    assert development.effective_allowed_hosts == [
        "localhost",
        "127.0.0.1",
        "testserver",
    ]


def test_production_requires_oidc() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            environment="production",
            allowed_hosts=["api.example.com"],
            oidc_enabled=False,
        )


def test_production_hardening() -> None:
    with pytest.raises(ValidationError):
        make_production_settings(debug=True)
    with pytest.raises(ValidationError):
        make_production_settings(db_echo=True)
    with pytest.raises(ValidationError):
        make_production_settings(log_format="console")
    with pytest.raises(ValidationError):
        make_production_settings(allowed_hosts=["*"])
    with pytest.raises(ValidationError):
        make_production_settings(cors_allowed_origins=["*"])


def test_production_oidc_requires_https() -> None:
    with pytest.raises(ValidationError):
        make_production_settings(oidc_issuer="http://idp.example.com")
    with pytest.raises(ValidationError):
        make_production_settings(oidc_jwks_uri="http://idp.example.com/jwks")


def test_docs_default_and_override() -> None:
    development = make_settings(environment="development")
    production = make_production_settings()
    overridden = make_production_settings(docs_enabled=True)

    assert development.api_docs_enabled is True
    assert production.api_docs_enabled is False
    assert overridden.api_docs_enabled is True


def test_oidc_enabled_requires_discovery_inputs() -> None:
    with pytest.raises(ValidationError):
        make_settings(oidc_enabled=True, oidc_issuer=None)
    with pytest.raises(ValidationError):
        make_settings(oidc_enabled=True, oidc_audience=None)
    with pytest.raises(ValidationError):
        make_settings(oidc_enabled=True, oidc_jwks_uri=None)


def test_oidc_algorithms_are_asymmetric_only() -> None:
    settings = make_settings(oidc_algorithms="rs256, ES256,RS256")
    assert settings.oidc_algorithms == ["RS256", "ES256"]
    for invalid in ("HS256", "none", "RS256,HS256", ""):
        with pytest.raises(ValidationError):
            make_settings(oidc_algorithms=invalid)


def test_oidc_claim_and_issuer_validation() -> None:
    with pytest.raises(ValidationError):
        make_settings(oidc_tenant_claim="bad claim")
    with pytest.raises(ValidationError):
        make_settings(oidc_issuer="https://user:password@idp.example.com")
    with pytest.raises(ValidationError):
        make_settings(oidc_issuer="not-a-url")

    settings = make_settings(oidc_issuer="https://idp.example.com/")
    assert settings.oidc_issuer == "https://idp.example.com/"


def test_cors_origins_must_be_origins() -> None:
    for invalid in (
        "https://frontend.example.com/app",
        "ftp://frontend.example.com",
        "https://user:password@frontend.example.com",
        "https://frontend.example.com?tenant=1",
        "https://frontend.example.com#fragment",
    ):
        with pytest.raises(ValidationError):
            make_settings(cors_allowed_origins=[invalid])

    settings = make_settings(cors_allowed_origins=["HTTPS://Frontend.example.com/"])
    assert settings.cors_allowed_origins == ["https://frontend.example.com"]


def test_cors_credentials_reject_wildcard() -> None:
    with pytest.raises(ValidationError):
        make_settings(cors_allowed_origins=["*"], cors_allow_credentials=True)


def test_api_prefix_validation() -> None:
    with pytest.raises(ValidationError):
        make_settings(api_prefix="api/v1")
    with pytest.raises(ValidationError):
        make_settings(api_prefix="/api/v1/")


def test_default_database_url_is_test_scoped() -> None:
    assert "test" in DEFAULT_TEST_DATABASE_URL

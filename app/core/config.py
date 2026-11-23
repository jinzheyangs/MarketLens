"""Application configuration.

All configuration is read from environment variables (optionally a local ``.env``
file for development). Secrets are never given code defaults and never appear in
``repr()`` output, error messages, or logs.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field, PostgresDsn, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app import __version__

Environment = Literal["development", "test", "staging", "production"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CLAIM_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

# Symmetric algorithms ("HS*") and "none" are intentionally not supported:
# the resource server must not share a signing secret with the identity provider.
_ALLOWED_OIDC_ALGORITHMS = frozenset(
    {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}
)


def _validate_service_url(value: str | None, setting_name: str) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None

    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{setting_name} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(
            f"{setting_name} must not embed credentials; use a dedicated secret setting"
        )
    return candidate.rstrip("/")


class Settings(BaseSettings):
    """Runtime settings.

    Nested service calls (Qdrant, OTLP) are configured with operator-controlled
    URLs, never with user-supplied URLs.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
    )

    app_name: str = "Market Intelligence Backend"
    version: str = __version__
    environment: Environment = "development"
    debug: bool = False
    docs_enabled: bool | None = None
    api_prefix: str = "/api/v1"

    # Database. ``repr=False`` prevents accidental credential disclosure when a
    # Settings instance is logged or included in an error message.
    database_url: PostgresDsn = Field(repr=False)
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    db_pool_recycle_seconds: int = Field(default=1800, ge=60)
    db_statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=600_000)
    db_application_name: str = "market-intel-backend"
    db_echo: bool = False

    # Vector store. Optional until the RAG stage; when configured it is part of
    # readiness.
    qdrant_url: str | None = None
    qdrant_api_key: SecretStr | None = Field(default=None, repr=False)
    qdrant_timeout_seconds: float = Field(default=3.0, gt=0, le=60)

    # HTTP / API behavior.
    log_level: LogLevel = "INFO"
    log_format: Literal["json", "console"] = "json"
    request_id_header: str = "X-Request-ID"
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    request_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    health_check_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    # NoDecode keeps raw dotenv/env values intact so the validator below can
    # accept either a JSON array or a comma-separated list. Without it,
    # pydantic-settings attempts JSON decoding at the source layer first.
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    cors_allow_credentials: bool = False
    # Empty means "use local development defaults" outside production.
    # Production must configure this explicitly (enforced below).
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # OIDC resource-server boundary. Browser login is performed by the frontend
    # or identity gateway; this backend verifies bearer JWTs against the issuer's
    # JWKS. No client secret or password is stored.
    oidc_enabled: bool = False
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_uri: str | None = None
    oidc_algorithms: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["RS256"])
    oidc_tenant_claim: str = "tenant_id"
    oidc_leeway_seconds: int = Field(default=60, ge=0, le=600)
    oidc_jwks_cache_seconds: int = Field(default=300, ge=0, le=86_400)
    oidc_http_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # Observability.
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "market-intel-backend"
    otel_traces_sampler_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    otel_export_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @field_validator("environment", "log_format", mode="before")
    @classmethod
    def _normalize_lower(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_upper(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("qdrant_api_key", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("oidc_issuer")
    @classmethod
    def _validate_issuer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        parsed = urlparse(candidate)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OIDC_ISSUER must be an absolute http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OIDC_ISSUER must not contain credentials, query, or fragment")
        # Trailing slashes are significant in issuer identifiers; preserve them.
        return candidate

    @field_validator("oidc_audience")
    @classmethod
    def _normalize_audience(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        return candidate or None

    @field_validator("oidc_jwks_uri")
    @classmethod
    def _validate_jwks_uri(cls, value: str | None) -> str | None:
        return _validate_service_url(value, "OIDC_JWKS_URI")

    @field_validator("oidc_tenant_claim")
    @classmethod
    def _validate_tenant_claim(cls, value: str) -> str:
        if not _CLAIM_NAME_PATTERN.fullmatch(value):
            raise ValueError("OIDC_TENANT_CLAIM must be a valid claim name")
        return value

    @field_validator("oidc_algorithms")
    @classmethod
    def _validate_algorithms(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            algorithm = value.strip().upper()
            if algorithm not in _ALLOWED_OIDC_ALGORITHMS:
                raise ValueError(
                    "OIDC_ALGORITHMS may only contain asymmetric algorithms "
                    f"({', '.join(sorted(_ALLOWED_OIDC_ALGORITHMS))})"
                )
            if algorithm not in normalized:
                normalized.append(algorithm)
        if not normalized:
            raise ValueError("OIDC_ALGORITHMS must not be empty")
        return normalized

    @field_validator("cors_allowed_origins", "allowed_hosts", "oidc_algorithms", mode="before")
    @classmethod
    def _parse_string_list(cls, value: Any) -> Any:
        """Accept a JSON array or a comma-separated environment variable."""
        if value is None:
            return []
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError("must be a JSON array or a comma-separated list") from exc
                if not isinstance(parsed, list):
                    raise ValueError("must be a JSON array or a comma-separated list")
                return parsed
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator("cors_allowed_origins")
    @classmethod
    def _validate_origins(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            if value == "*":
                normalized.append(value)
                continue
            parsed = urlparse(value)
            scheme = parsed.scheme.lower()
            try:
                port = parsed.port
            except ValueError as exc:
                raise ValueError("CORS origin contains an invalid port") from exc
            if (
                scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "CORS origins must be absolute http(s) origins without paths, "
                    "credentials, queries, or fragments"
                )
            host = parsed.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
                port = None
            port_suffix = f":{port}" if port is not None else ""
            normalized.append(f"{scheme}://{host}{port_suffix}")
        return normalized

    @field_validator("qdrant_url")
    @classmethod
    def _validate_qdrant_url(cls, value: str | None) -> str | None:
        return _validate_service_url(value, "QDRANT_URL")

    @field_validator("otel_exporter_otlp_endpoint")
    @classmethod
    def _validate_otel_endpoint(cls, value: str | None) -> str | None:
        return _validate_service_url(value, "OTEL_EXPORTER_OTLP_ENDPOINT")

    @field_validator("database_url")
    @classmethod
    def _require_async_postgres(cls, value: PostgresDsn) -> PostgresDsn:
        if value.scheme != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg scheme")
        return value

    @field_validator("api_prefix")
    @classmethod
    def _validate_api_prefix(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("API_PREFIX must start with '/'")
        if len(value) > 1 and value.endswith("/"):
            raise ValueError("API_PREFIX must not end with '/'")
        return value

    @field_validator("request_id_header")
    @classmethod
    def _validate_request_id_header(cls, value: str) -> str:
        if not _HEADER_NAME_PATTERN.fullmatch(value):
            raise ValueError("REQUEST_ID_HEADER must be a valid HTTP header name")
        return value

    @model_validator(mode="after")
    def _validate_cross_field_constraints(self) -> Settings:
        if self.cors_allow_credentials and "*" in self.cors_allowed_origins:
            raise ValueError("CORS_ALLOW_CREDENTIALS cannot be combined with a wildcard origin")
        if self.qdrant_api_key is not None and self.qdrant_url is None:
            raise ValueError("QDRANT_API_KEY requires QDRANT_URL")
        if self.otel_enabled and not self.otel_exporter_otlp_endpoint:
            raise ValueError("OTEL_ENABLED requires OTEL_EXPORTER_OTLP_ENDPOINT")

        if self.oidc_enabled:
            missing = [
                name
                for name, value in (
                    ("OIDC_ISSUER", self.oidc_issuer),
                    ("OIDC_AUDIENCE", self.oidc_audience),
                    ("OIDC_JWKS_URI", self.oidc_jwks_uri),
                )
                if not value
            ]
            if missing:
                raise ValueError("OIDC_ENABLED requires " + ", ".join(missing))

        if self.environment == "production":
            if not self.oidc_enabled:
                raise ValueError("OIDC_ENABLED must be true in production")
            if self.debug:
                raise ValueError("DEBUG must be disabled in production")
            if self.db_echo:
                raise ValueError("DB_ECHO must be disabled in production")
            if self.log_format != "json":
                raise ValueError("LOG_FORMAT must be 'json' in production")
            if not self.allowed_hosts:
                raise ValueError("ALLOWED_HOSTS must be configured in production")
            if "*" in self.allowed_hosts:
                raise ValueError("ALLOWED_HOSTS must not contain '*' in production")
            if "*" in self.cors_allowed_origins:
                raise ValueError("CORS_ALLOWED_ORIGINS must not contain '*' in production")
            if self.oidc_issuer and urlparse(self.oidc_issuer).scheme != "https":
                raise ValueError("OIDC_ISSUER must use https in production")
            if self.oidc_jwks_uri and urlparse(self.oidc_jwks_uri).scheme != "https":
                raise ValueError("OIDC_JWKS_URI must use https in production")
        return self

    @property
    def database_url_str(self) -> str:
        """Full DSN for the database driver. Never log this value."""
        return str(self.database_url)

    @property
    def safe_database_url(self) -> str:
        """Credential-free database label safe for startup logs."""
        parsed = urlparse(str(self.database_url))
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        database = (parsed.path or "").lstrip("/")
        return f"{parsed.scheme}://{host}{port}/{database}"

    @property
    def effective_allowed_hosts(self) -> list[str]:
        if self.allowed_hosts:
            return self.allowed_hosts
        if self.environment == "production":
            return []
        return ["localhost", "127.0.0.1", "testserver"]

    @property
    def api_docs_enabled(self) -> bool:
        if self.docs_enabled is not None:
            return self.docs_enabled
        return self.environment != "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return process-wide settings. Tests should construct ``Settings`` directly."""
    return Settings()

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
_QDRANT_COLLECTION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_S3_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")

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

    # Document ingestion / RAG. Disabled by default; enabling exposes the
    # document and search routes and requires storage/embedding/vector config.
    rag_enabled: bool = False
    object_storage_backend: Literal["local", "s3"] = "local"
    object_storage_local_path: str = ".data/objects"
    document_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    document_max_parsed_chars: int = Field(default=2_000_000, ge=1_000, le=50_000_000)
    document_max_pages: int = Field(default=500, ge=1, le=5_000)
    document_max_chunks: int = Field(default=5_000, ge=1, le=100_000)
    document_state_lease_seconds: int = Field(default=600, ge=30, le=86_400)
    chunk_max_chars: int = Field(default=1000, ge=100, le=8_000)
    chunk_overlap_chars: int = Field(default=100, ge=0, le=4_000)

    embedding_provider: Literal["hash", "http"] = "hash"
    embedding_model: str = "hash-v1"
    embedding_version: str = "1"
    embedding_dimension: int = Field(default=256, ge=8, le=4_096)
    embedding_api_url: str | None = None
    embedding_api_key: SecretStr | None = Field(default=None, repr=False)
    embedding_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    embedding_batch_size: int = Field(default=32, ge=1, le=256)
    embedding_max_retries: int = Field(default=2, ge=0, le=10)

    qdrant_collection: str = "document_chunks"
    qdrant_local_mode: bool = False

    s3_endpoint_url: str | None = None
    s3_bucket: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr | None = Field(default=None, repr=False)
    s3_secret_key: SecretStr | None = Field(default=None, repr=False)
    s3_use_ssl: bool = True
    s3_addressing_style: Literal["auto", "path", "virtual"] = "auto"
    s3_auto_create_bucket: bool = False
    s3_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    search_candidate_limit: int = Field(default=50, ge=10, le=200)
    search_default_limit: int = Field(default=10, ge=1, le=50)
    search_max_limit: int = Field(default=50, ge=1, le=200)

    # External search and isolated web collection.
    collector_enabled: bool = False
    collector_user_agent: str = "MarketLensCollector/0.1 (+https://example.invalid/collector)"
    collector_allowed_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    collector_blocked_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)
    collector_allow_private_networks: bool = False
    collector_request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    collector_max_response_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)
    collector_max_extracted_chars: int = Field(default=200_000, ge=1_000, le=2_000_000)
    collector_max_redirects: int = Field(default=3, ge=0, le=10)
    collector_allowed_ports: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [80, 443]
    )
    collector_allowed_content_types: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["text/html", "text/plain", "application/xhtml+xml"]
    )
    collector_respect_robots: bool = True
    collector_robots_cache_seconds: int = Field(default=3600, ge=0, le=86_400)
    collector_robots_max_bytes: int = Field(default=512_000, ge=1024, le=5_000_000)
    collector_max_concurrent_tasks: int = Field(default=4, ge=1, le=32)
    collector_poll_interval_seconds: float = Field(default=2.0, ge=0.1, le=60)
    collector_task_lease_seconds: int = Field(default=120, ge=30, le=3_600)
    collector_max_attempts: int = Field(default=3, ge=1, le=10)
    collector_retry_base_seconds: float = Field(default=5.0, ge=0.1, le=300)
    collector_retry_max_seconds: float = Field(default=300.0, ge=1, le=3_600)
    collector_domain_min_interval_seconds: float = Field(default=1.0, ge=0, le=60)
    collector_max_tasks_per_request: int = Field(default=20, ge=1, le=100)

    web_search_provider: Literal["none", "http"] = "none"
    web_search_api_url: str | None = None
    web_search_api_key: SecretStr | None = Field(default=None, repr=False)
    web_search_api_key_header: str = "X-API-Key"
    web_search_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    web_search_max_results: int = Field(default=10, ge=1, le=50)

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

    @field_validator(
        "qdrant_api_key",
        "embedding_api_key",
        "s3_access_key",
        "s3_secret_key",
        "web_search_api_key",
        mode="before",
    )
    @classmethod
    def _empty_secret_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("embedding_api_url")
    @classmethod
    def _validate_embedding_url(cls, value: str | None) -> str | None:
        return _validate_service_url(value, "EMBEDDING_API_URL")

    @field_validator("s3_endpoint_url")
    @classmethod
    def _validate_s3_endpoint(cls, value: str | None) -> str | None:
        return _validate_service_url(value, "S3_ENDPOINT_URL")

    @field_validator("s3_bucket")
    @classmethod
    def _validate_s3_bucket(cls, value: str | None) -> str | None:
        if value is None:
            return None
        candidate = value.strip()
        if not candidate:
            return None
        if ".." in candidate or not _S3_BUCKET_PATTERN.fullmatch(candidate):
            raise ValueError("S3_BUCKET must be a valid lowercase S3 bucket name")
        return candidate

    @field_validator("qdrant_collection")
    @classmethod
    def _validate_qdrant_collection(cls, value: str) -> str:
        if not _QDRANT_COLLECTION_PATTERN.fullmatch(value):
            raise ValueError("QDRANT_COLLECTION must contain only letters, digits, '_' or '-'")
        return value

    @field_validator(
        "object_storage_local_path",
        "embedding_model",
        "embedding_version",
        "s3_region",
    )
    @classmethod
    def _require_non_empty_text(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate:
            raise ValueError("must not be empty")
        return candidate

    @field_validator("web_search_api_url")
    @classmethod
    def _validate_web_search_url(cls, value: str | None) -> str | None:
        return _validate_service_url(value, "WEB_SEARCH_API_URL")

    @field_validator("web_search_api_key_header")
    @classmethod
    def _validate_api_key_header(cls, value: str) -> str:
        if not _HEADER_NAME_PATTERN.fullmatch(value):
            raise ValueError("WEB_SEARCH_API_KEY_HEADER must be a valid header name")
        return value

    @field_validator("collector_user_agent")
    @classmethod
    def _validate_user_agent(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate or len(candidate) > 256 or "\r" in candidate or "\n" in candidate:
            raise ValueError("COLLECTOR_USER_AGENT must be a safe, non-empty value")
        return candidate

    @field_validator("collector_allowed_domains", "collector_blocked_domains")
    @classmethod
    def _normalize_domain_policy(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            candidate = value.strip().lower()
            if candidate.startswith("*."):
                candidate = candidate[2:]
            candidate = candidate.rstrip(".")
            if not candidate or "/" in candidate or "\\" in candidate:
                raise ValueError("domain policy entries must be host suffixes")
            if candidate not in normalized:
                normalized.append(candidate)
        return normalized

    @field_validator("collector_allowed_ports")
    @classmethod
    def _validate_allowed_ports(cls, values: list[int]) -> list[int]:
        if not values:
            raise ValueError("COLLECTOR_ALLOWED_PORTS must not be empty")
        normalized: list[int] = []
        for value in values:
            port = int(value)
            if not 1 <= port <= 65_535:
                raise ValueError("COLLECTOR_ALLOWED_PORTS contains an invalid port")
            if port not in normalized:
                normalized.append(port)
        return sorted(normalized)

    @field_validator("collector_allowed_content_types")
    @classmethod
    def _validate_content_types(cls, values: list[str]) -> list[str]:
        allowed = {"text/html", "text/plain", "application/xhtml+xml"}
        normalized: list[str] = []
        for value in values:
            candidate = value.split(";", 1)[0].strip().lower()
            if not candidate or candidate not in allowed:
                raise ValueError("COLLECTOR_ALLOWED_CONTENT_TYPES contains an unsupported type")
            if candidate not in normalized:
                normalized.append(candidate)
        if not normalized:
            raise ValueError("COLLECTOR_ALLOWED_CONTENT_TYPES must not be empty")
        return normalized

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

    @field_validator(
        "cors_allowed_origins",
        "allowed_hosts",
        "oidc_algorithms",
        "collector_allowed_domains",
        "collector_blocked_domains",
        "collector_allowed_ports",
        "collector_allowed_content_types",
        mode="before",
    )
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

        if self.chunk_overlap_chars >= self.chunk_max_chars:
            raise ValueError("CHUNK_OVERLAP_CHARS must be less than CHUNK_MAX_CHARS")
        if self.search_default_limit > self.search_max_limit:
            raise ValueError("SEARCH_DEFAULT_LIMIT must not exceed SEARCH_MAX_LIMIT")
        if (self.s3_access_key is None) != (self.s3_secret_key is None):
            raise ValueError("S3_ACCESS_KEY and S3_SECRET_KEY must be configured together")
        if self.s3_endpoint_url:
            endpoint_scheme = urlparse(self.s3_endpoint_url).scheme
            if endpoint_scheme == "https" and not self.s3_use_ssl:
                raise ValueError("S3_USE_SSL must be true for an https S3 endpoint")
            if endpoint_scheme == "http" and self.s3_use_ssl:
                raise ValueError("S3_USE_SSL must be false for an http S3 endpoint")

        if self.rag_enabled:
            if not self.qdrant_local_mode and not self.qdrant_url:
                raise ValueError("RAG_ENABLED requires QDRANT_URL or QDRANT_LOCAL_MODE")
            if self.embedding_provider == "http" and not self.embedding_api_url:
                raise ValueError("EMBEDDING_PROVIDER=http requires EMBEDDING_API_URL")
            if self.object_storage_backend == "s3" and not self.s3_bucket:
                raise ValueError("OBJECT_STORAGE_BACKEND=s3 requires S3_BUCKET")

        if self.collector_allow_private_networks and self.environment not in {
            "development",
            "test",
        }:
            raise ValueError("COLLECTOR_ALLOW_PRIVATE_NETWORKS is only allowed in development/test")
        if self.collector_retry_base_seconds > self.collector_retry_max_seconds:
            raise ValueError(
                "COLLECTOR_RETRY_BASE_SECONDS must not exceed COLLECTOR_RETRY_MAX_SECONDS"
            )
        if self.web_search_provider == "http" and not self.web_search_api_url:
            raise ValueError("WEB_SEARCH_PROVIDER=http requires WEB_SEARCH_API_URL")
        if self.collector_enabled and self.object_storage_backend == "s3" and not self.s3_bucket:
            raise ValueError("COLLECTOR_ENABLED with S3 requires S3_BUCKET")

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
            if self.qdrant_local_mode:
                raise ValueError("QDRANT_LOCAL_MODE must be disabled in production")
            if self.s3_auto_create_bucket:
                raise ValueError("S3_AUTO_CREATE_BUCKET must be disabled in production")
            if self.rag_enabled:
                if self.object_storage_backend != "s3":
                    raise ValueError("RAG_ENABLED requires OBJECT_STORAGE_BACKEND=s3 in production")
                if self.embedding_provider != "http":
                    raise ValueError("RAG_ENABLED requires EMBEDDING_PROVIDER=http in production")
                if not self.s3_use_ssl:
                    raise ValueError("S3_USE_SSL must be true in production")
                if self.embedding_api_url and urlparse(self.embedding_api_url).scheme != "https":
                    raise ValueError("EMBEDDING_API_URL must use https in production")
                if self.s3_endpoint_url and urlparse(self.s3_endpoint_url).scheme != "https":
                    raise ValueError("S3_ENDPOINT_URL must use https in production")
            if self.collector_allow_private_networks:
                raise ValueError("COLLECTOR_ALLOW_PRIVATE_NETWORKS must be false in production")
            if self.collector_enabled:
                if self.object_storage_backend != "s3":
                    raise ValueError(
                        "COLLECTOR_ENABLED requires OBJECT_STORAGE_BACKEND=s3 in production"
                    )
                if not self.s3_bucket:
                    raise ValueError("COLLECTOR_ENABLED requires S3_BUCKET in production")
                if not self.s3_use_ssl:
                    raise ValueError("S3_USE_SSL must be true in production")
                if self.s3_endpoint_url and urlparse(self.s3_endpoint_url).scheme != "https":
                    raise ValueError("S3_ENDPOINT_URL must use https in production")
                if any(port not in {80, 443} for port in self.collector_allowed_ports):
                    raise ValueError(
                        "COLLECTOR_ALLOWED_PORTS must be limited to 80/443 in production"
                    )
            if (
                self.web_search_provider == "http"
                and self.web_search_api_url
                and urlparse(self.web_search_api_url).scheme != "https"
            ):
                raise ValueError("WEB_SEARCH_API_URL must use https in production")
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

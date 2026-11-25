"""结构化日志：支持请求/租户关联与敏感信息脱敏。

绝不记录请求体、查询串、请求头和数据库 DSN。调用方可通过
``logger.info(..., extra={"context": {...}})`` 挂结构化上下文；
该映射中的敏感键会被递归脱敏。
"""

from __future__ import annotations

import atexit
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from pydantic import SecretStr

from app.core.config import Settings
from app.core.context import current_request_id, current_tenant_id
from app.core.telemetry import _logs_endpoint

# 脱敏占位符
REDACTED = "[REDACTED]"
_MAX_CONTEXT_DEPTH = 6  # 上下文递归脱敏的最大深度
_MAX_STRING_LENGTH = 500  # 超长字符串截断阈值

# 命中即视为敏感的键名片段（小写、下划线归一后匹配）
_SENSITIVE_KEY_PARTS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "cookie",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
        "database_url",
        "dsn",
        "credential",
    }
)


def _is_sensitive_key(key: str) -> bool:
    """判断键名是否属于敏感字段。"""
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """返回 ``value`` 的 JSON 安全副本，敏感字段被脱敏、超长内容被截断。"""
    if _depth > _MAX_CONTEXT_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            redacted[key_str] = (
                REDACTED if _is_sensitive_key(key_str) else redact(item, _depth=_depth + 1)
            )
        return redacted
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str) and len(value) > _MAX_STRING_LENGTH:
            return f"{value[:_MAX_STRING_LENGTH]}...[TRUNCATED]"
        return value
    rendered = repr(value)
    if len(rendered) > _MAX_STRING_LENGTH:
        rendered = f"{rendered[:_MAX_STRING_LENGTH]}...[TRUNCATED]"
    return rendered


def _trace_fields() -> dict[str, str]:
    """从当前 OTel Span 提取 trace_id / span_id（无有效 Span 时返回空）。"""
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return {}
    return {
        "trace_id": format(span_context.trace_id, "032x"),
        "span_id": format(span_context.span_id, "016x"),
    }


def _base_fields(record: logging.LogRecord) -> dict[str, Any]:
    """组装日志公共字段：时间、级别、logger、消息、请求/租户/追踪关联。"""
    fields: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
    }
    request_id = current_request_id()
    if request_id is not None:
        fields["request_id"] = request_id
    tenant_id = current_tenant_id()
    if tenant_id is not None:
        fields["tenant_id"] = str(tenant_id)
    fields.update(_trace_fields())
    context = getattr(record, "context", None)
    if context is not None:
        fields["context"] = redact(context)
    return fields


class _RedactionFilter(logging.Filter):
    """在任何处理器（文件、stdout、OTLP）输出前先脱敏结构化上下文。"""

    def filter(self, record: logging.LogRecord) -> bool:
        context = getattr(record, "context", None)
        if context is not None:
            record.context = redact(context)
        return True


class JsonFormatter(logging.Formatter):
    """输出单行 JSON 日志（机器可读）。"""

    def format(self, record: logging.LogRecord) -> str:
        fields = _base_fields(record)
        if record.exc_info:
            fields["exception"] = self.formatException(record.exc_info)
        return json.dumps(fields, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    """输出人类可读的单行控制台日志。"""

    def format(self, record: logging.LogRecord) -> str:
        fields = _base_fields(record)
        correlation = ""
        if "request_id" in fields:
            correlation = f" request_id={fields['request_id']}"
        if "tenant_id" in fields:
            correlation += f" tenant_id={fields['tenant_id']}"
        if "context" in fields:
            correlation += f" context={json.dumps(fields['context'], ensure_ascii=False)}"
        line = (
            f"{fields['timestamp']} {fields['level']} {fields['logger']} "
            f"{fields['message']}{correlation}"
        )
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure_logging(settings: Settings) -> None:
    """按进程/应用构建一次性配置根日志。

    - 装配 stdout 处理器（JSON 或控制台格式）与脱敏过滤器；
    - 可选接入 OTLP 日志导出；
    - 收敛 uvicorn 与第三方库日志级别，避免访问日志泄露查询串/请求头。
    """
    redaction = _RedactionFilter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_format == "json" else ConsoleFormatter())
    handler.addFilter(redaction)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # 可选：OTel 日志批量导出
    if settings.otel_logs_enabled and settings.otel_exporter_otlp_endpoint is not None:
        provider = LoggerProvider(
            resource=Resource.create(
                {
                    "service.name": settings.otel_service_name,
                    "service.version": settings.version,
                    "deployment.environment.name": settings.environment,
                }
            )
        )
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(
                    endpoint=_logs_endpoint(settings.otel_exporter_otlp_endpoint),
                    timeout=settings.otel_export_timeout_seconds,
                )
            )
        )
        otel_handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        otel_handler.addFilter(redaction)
        root.addHandler(otel_handler)
        atexit.register(provider.shutdown)

    # uvicorn 在导入应用前已装好自己的处理器；这里改走结构化根处理器。
    # 访问日志由本项目中间件输出，且不带查询串/请求头。
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").disabled = True

    # 第三方库日志级别收敛
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.db_echo else logging.WARNING
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """获取命名日志器。"""
    return logging.getLogger(name)

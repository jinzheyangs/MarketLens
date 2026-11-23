from __future__ import annotations

import json
import logging
from uuid import uuid4

from app.core.context import tenant_context
from app.core.errors import TenantContextError
from app.core.logging import JsonFormatter, redact


def _record(message: str = "hello", context: object | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test.logger",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    if context is not None:
        record.context = context
    return record


def test_redact_recursively_masks_secrets() -> None:
    redacted = redact(
        {
            "api_key": "secret",
            "nested": {
                "password": "pw",
                "authorization": "Bearer x",
                "access_key": "ak",
                "safe": "ok",
            },
            "items": [{"refresh_token": "t"}],
        }
    )
    assert redacted == {
        "api_key": "[REDACTED]",
        "nested": {
            "password": "[REDACTED]",
            "authorization": "[REDACTED]",
            "access_key": "[REDACTED]",
            "safe": "ok",
        },
        "items": [{"refresh_token": "[REDACTED]"}],
    }


def test_json_formatter_includes_correlation_fields() -> None:
    tenant_id = uuid4()
    with tenant_context(tenant_id):
        payload = json.loads(JsonFormatter().format(_record(context={"status": 200})))
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.logger"
    assert payload["message"] == "hello"
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["context"] == {"status": 200}
    assert payload["timestamp"].endswith("Z")


def test_json_formatter_redacts_context_and_serializes_objects() -> None:
    payload = json.loads(JsonFormatter().format(_record(context={"api_key": "x", "obj": object()})))
    assert payload["context"]["api_key"] == "[REDACTED]"
    assert "object object" in payload["context"]["obj"]


def test_json_formatter_does_not_emit_exception_message_in_message_field() -> None:
    try:
        raise ValueError("internal detail")
    except ValueError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "hello"
    assert "internal detail" in payload["exception"]


def test_tenant_errors_are_application_errors() -> None:
    error = TenantContextError()
    assert error.status_code == 500
    assert error.code == "tenant_context_required"

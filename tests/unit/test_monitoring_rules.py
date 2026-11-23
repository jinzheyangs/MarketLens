from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.monitoring.crypto import ChannelSecretCipher
from app.monitoring.enums import Severity
from app.monitoring.errors import ChannelSecretError, NotificationTargetError
from app.monitoring.service import severity_at_least
from app.monitoring.webhook import SafeWebhookClient
from tests.conftest import make_settings


def test_severity_ordering() -> None:
    assert severity_at_least(Severity.CRITICAL, Severity.MEDIUM)
    assert severity_at_least(Severity.HIGH, Severity.HIGH)
    assert not severity_at_least(Severity.LOW, Severity.MEDIUM)
    assert not severity_at_least("unknown", Severity.LOW)


def test_channel_secret_cipher_round_trip_and_tamper_detection() -> None:
    key = Fernet.generate_key().decode()
    settings = make_settings(notification_encryption_key=key)
    cipher = ChannelSecretCipher(settings)
    secret = {
        "url": "https://hooks.example.com/marketlens",
        "signing_secret": "top-secret-value",
    }
    token = cipher.encrypt(secret)
    assert "hooks.example.com" not in token
    assert "top-secret-value" not in token
    assert cipher.decrypt(token) == secret

    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    with pytest.raises(ChannelSecretError):
        cipher.decrypt(tampered)

    with pytest.raises(ChannelSecretError):
        ChannelSecretCipher(make_settings())


def test_webhook_target_validation_rejects_unsafe_destinations() -> None:
    client = SafeWebhookClient(
        make_settings(
            notification_allowed_domains=["hooks.example.com"],
        )
    )
    safe = client.validate_target("https://hooks.example.com/events")
    assert safe.host == "hooks.example.com"

    with pytest.raises(NotificationTargetError):
        client.validate_target("http://hooks.example.com/events")
    with pytest.raises(NotificationTargetError):
        client.validate_target("https://other.example.com/events")
    with pytest.raises(NotificationTargetError):
        client.validate_target("https://127.0.0.1/events")
    with pytest.raises(NotificationTargetError):
        client.validate_target("https://169.254.169.254/latest/meta-data")
    with pytest.raises(NotificationTargetError):
        client.validate_target("https://user:password@hooks.example.com/events")


def test_webhook_http_requires_explicit_development_opt_in() -> None:
    client = SafeWebhookClient(
        make_settings(
            notification_allow_http=True,
            notification_allowed_domains=["internal.example.com"],
        )
    )
    safe = client.validate_target("http://internal.example.com/hook")
    assert safe.scheme == "http"

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


def test_channel_secret_cipher_previous_key_rotates_ciphertext() -> None:
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    old_settings = make_settings(notification_encryption_key=old_key)
    rotating = make_settings(
        notification_encryption_key=new_key,
        notification_previous_encryption_key=old_key,
    )
    secret = {"url": "https://hooks.example.com/marketlens"}

    old_token = ChannelSecretCipher(old_settings).encrypt(secret)
    rotation_cipher = ChannelSecretCipher(rotating)
    assert rotation_cipher.decrypt(old_token) == secret
    new_token = rotation_cipher.encrypt(secret)
    assert new_token != old_token

    # The old-only deployment can no longer read the re-encrypted value.
    with pytest.raises(ChannelSecretError):
        ChannelSecretCipher(old_settings).decrypt(new_token)


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


def test_slack_text_escapes_mentions_and_links() -> None:
    from app.monitoring.worker import _slack_escape

    escaped = _slack_escape("<!channel> see <https://evil.example|click> & more")
    assert "<" not in escaped
    assert ">" not in escaped
    assert "&amp;" in escaped


def test_email_message_sanitizes_headers_and_sets_delivery_id() -> None:
    from app.monitoring.email import build_email_message

    message = build_email_message(
        sender="alerts@example.com",
        recipients=["ops@example.com"],
        subject="[MarketLens] summary\r\nBcc: attacker@example.com",
        body="Body with source https://example.test/page",
        delivery_id="delivery-123",
    )
    assert "\n" not in message["Subject"]
    assert "\r" not in message["Subject"]
    assert message["Message-ID"] == "<delivery-123@marketlens.local>"
    assert message["X-MarketLens-Delivery"] == "delivery-123"
    assert message.get_content().startswith("Body with source")

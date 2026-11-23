"""At-rest encryption for tenant notification channel configuration."""

from __future__ import annotations

import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import Settings
from app.monitoring.errors import ChannelSecretError


class ChannelSecretCipher:
    def __init__(self, settings: Settings) -> None:
        if settings.notification_encryption_key is None:
            raise ChannelSecretError
        try:
            self._fernet = Fernet(
                settings.notification_encryption_key.get_secret_value().encode("ascii")
            )
        except (ValueError, UnicodeEncodeError) as exc:
            raise ChannelSecretError from exc

    def encrypt(self, value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self._fernet.encrypt(raw).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            raw = self._fernet.decrypt(token.encode("ascii"))
            decoded = json.loads(raw)
        except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChannelSecretError from exc
        if not isinstance(decoded, dict):
            raise ChannelSecretError
        return decoded


__all__ = ["ChannelSecretCipher"]

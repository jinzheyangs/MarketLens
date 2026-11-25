"""租户通知渠道配置的静态加密。"""

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
        keys = [settings.notification_encryption_key]
        if settings.notification_previous_encryption_key is not None:
            keys.append(settings.notification_previous_encryption_key)
        try:
            self._fernets = [Fernet(key.get_secret_value().encode("ascii")) for key in keys]
        except (ValueError, UnicodeEncodeError) as exc:
            raise ChannelSecretError from exc
        self._primary = self._fernets[0]

    def encrypt(self, value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self._primary.encrypt(raw).decode("ascii")

    def decrypt(self, token: str) -> dict[str, Any]:
        decoded: object = None
        last_error: Exception | None = None
        for fernet in self._fernets:
            try:
                raw = fernet.decrypt(token.encode("ascii"))
                decoded = json.loads(raw)
                break
            except (
                InvalidToken,
                ValueError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                continue
        if decoded is None or not isinstance(decoded, dict):
            raise ChannelSecretError from last_error
        return decoded


__all__ = ["ChannelSecretCipher"]

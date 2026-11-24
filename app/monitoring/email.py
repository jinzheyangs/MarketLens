"""Minimal, operator-configured SMTP notification adapter.

SMTP credentials and relay host come only from Settings (environment or a secret
manager). Recipient addresses are passed in from the encrypted channel config.
No recipient or credential values are logged.
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import Settings
from app.monitoring.errors import EmailNotConfiguredError, EmailSendError

_MAX_SUBJECT = 180


def _sanitize_header(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())[:_MAX_SUBJECT]


def build_email_message(
    *,
    sender: str,
    recipients: list[str],
    subject: str,
    body: str,
    delivery_id: str,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = _sanitize_header(subject)
    message["Message-ID"] = f"<{delivery_id}@marketscope.local>"
    message["X-MarketScope-Delivery"] = delivery_id
    message.set_content(body)
    return message


class EmailSender:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def available(self) -> bool:
        return self._settings.smtp_host is not None and self._settings.smtp_from is not None

    async def send(
        self,
        *,
        recipients: list[str],
        subject: str,
        body: str,
        delivery_id: str,
    ) -> int:
        if not self.available:
            raise EmailNotConfiguredError
        if not recipients or len(recipients) > self._settings.smtp_max_recipients:
            raise EmailSendError
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    self._send_sync,
                    recipients,
                    subject,
                    body,
                    delivery_id,
                ),
                timeout=self._settings.smtp_timeout_seconds + 5,
            )
        except TimeoutError as exc:
            raise EmailSendError from exc
        return 250

    def _send_sync(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        delivery_id: str,
    ) -> None:
        settings = self._settings
        if settings.smtp_host is None or settings.smtp_from is None:
            raise EmailNotConfiguredError
        message = build_email_message(
            sender=str(settings.smtp_from),
            recipients=recipients,
            subject=subject,
            body=body,
            delivery_id=delivery_id,
        )
        context = ssl.create_default_context()
        client: smtplib.SMTP
        try:
            if settings.smtp_ssl:
                client = smtplib.SMTP_SSL(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=settings.smtp_timeout_seconds,
                    context=context,
                )
            else:
                client = smtplib.SMTP(
                    settings.smtp_host,
                    settings.smtp_port,
                    timeout=settings.smtp_timeout_seconds,
                )
            with client:
                client.ehlo()
                if settings.smtp_starttls and not settings.smtp_ssl:
                    client.starttls(context=context)
                    client.ehlo()
                if settings.smtp_username is not None and settings.smtp_password is not None:
                    client.login(
                        settings.smtp_username,
                        settings.smtp_password.get_secret_value(),
                    )
                refused = client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailSendError from exc
        if refused:
            raise EmailSendError


__all__ = ["EmailSender", "build_email_message"]

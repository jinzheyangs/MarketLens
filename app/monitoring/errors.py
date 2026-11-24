"""Safe monitoring and notification errors."""

from __future__ import annotations


class MonitoringError(Exception):
    code = "monitoring_error"
    safe_message = "The monitoring operation failed."
    retryable = False


class ChannelSecretError(MonitoringError):
    code = "channel_secret_error"
    safe_message = "The notification channel secret could not be processed."


class NotificationSendError(MonitoringError):
    code = "notification_send_failed"
    safe_message = "The notification could not be delivered."
    retryable = True


class NotificationTargetError(MonitoringError):
    code = "notification_target_rejected"
    safe_message = "The notification target is not permitted."
    retryable = False


class EmailNotConfiguredError(MonitoringError):
    code = "email_not_configured"
    safe_message = "The email notification channel is not configured."
    retryable = False


class EmailSendError(MonitoringError):
    code = "email_send_failed"
    safe_message = "The email notification could not be delivered."
    retryable = True

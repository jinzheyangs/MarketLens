from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.rag import _lease_expired


def test_processing_lease_expiry() -> None:
    now = datetime.now(UTC)
    assert _lease_expired(None, 60) is True
    assert _lease_expired(now - timedelta(seconds=10), 60) is False
    assert _lease_expired(now - timedelta(seconds=120), 60) is True
    assert _lease_expired(now.replace(tzinfo=None) - timedelta(seconds=120), 60) is True

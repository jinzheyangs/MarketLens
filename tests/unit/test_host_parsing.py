from __future__ import annotations

import pytest

from app.core.middleware import parse_host_header


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("example.com", "example.com"),
        ("Example.COM:8443", "example.com"),
        ("[::1]", "[::1]"),
        ("[2001:db8::1]:8000", "[2001:db8::1]"),
    ],
)
def test_valid_host_headers(header: str, expected: str) -> None:
    assert parse_host_header(header) == expected


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "   ",
        "bank.example.com:not-a-port",
        "bank.example.com:80:90",
        "::1",
        "trusted.example.com/path",
        "trusted.example.com\\path",
        "evil.example.com@trusted.example.com",
        "bad host",
        "[::1",
        "[::1]junk",
    ],
)
def test_invalid_host_headers(header: str | None) -> None:
    assert parse_host_header(header) is None

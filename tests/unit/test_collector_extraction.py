from __future__ import annotations

import pytest

from app.collector.errors import ExtractionError
from app.collector.extraction import BasicContentExtractor
from tests.conftest import make_settings


def _extractor(**overrides):
    return BasicContentExtractor(make_settings(collector_max_extracted_chars=1000, **overrides))


def test_html_extraction_drops_scripts_and_keeps_title() -> None:
    body = (
        b"<html><head><title>Example &amp; Co</title>"
        b"<style>.x{color:red}</style></head><body>"
        b"<script>alert('x')</script><h1>Heading</h1><p>Hello <b>world</b>.</p>"
        b"<noscript>hidden</noscript></body></html>"
    )
    extracted = _extractor().extract(body, content_type="text/html", charset="utf-8")
    assert extracted.title == "Example & Co"
    assert "Heading" in extracted.text
    assert "Hello world" in extracted.text
    assert "alert" not in extracted.text
    assert "hidden" not in extracted.text
    assert extracted.truncated is False


def test_plain_text_and_charset_extraction() -> None:
    extracted = _extractor().extract(
        "café".encode("latin-1"),
        content_type="text/plain",
        charset="latin-1",
    )
    assert extracted.title is None
    assert extracted.text == "café"


def test_truncation_is_bounded() -> None:
    extracted = _extractor().extract(
        b"<p>" + b"x" * 2000 + b"</p>",
        content_type="text/html",
        charset=None,
    )
    assert extracted.truncated is True
    assert len(extracted.text) == 1000


def test_empty_or_unsupported_content_is_rejected() -> None:
    with pytest.raises(ExtractionError):
        _extractor().extract(b"<script>only</script>", content_type="text/html", charset=None)
    with pytest.raises(ExtractionError):
        _extractor().extract(b"{}", content_type="application/json", charset=None)

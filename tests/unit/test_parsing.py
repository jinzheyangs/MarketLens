from __future__ import annotations

import pytest
from fpdf import FPDF

from app.rag.errors import (
    DocumentParseError,
    DocumentTooLargeError,
    UnsupportedDocumentError,
)
from app.rag.parsing import (
    parse_document,
    sanitize_filename,
    sanitize_title,
    validate_upload,
)
from tests.conftest import make_settings

_SETTINGS = make_settings(
    document_max_bytes=4096,
    document_max_parsed_chars=5000,
    document_max_pages=5,
)


def _pdf_bytes(pages: int = 1) -> bytes:
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    for index in range(pages):
        pdf.add_page()
        pdf.cell(0, 10, f"Hello PDF page {index + 1}")
    return bytes(pdf.output())


def test_sanitize_filename_and_title() -> None:
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("bad\x00name.txt") == "badname.txt"
    assert sanitize_filename("x" * 400 + ".txt").endswith(".txt")
    assert len(sanitize_filename("x" * 400 + ".txt")) <= 255
    assert sanitize_title(None, "report.pdf") == "report"
    assert sanitize_title("  My title  ", "x.txt") == "My title"


def test_text_upload_validation_and_parsing() -> None:
    data = b"Hello world.\n\nSecond paragraph."
    upload = validate_upload(
        filename="notes.txt",
        content_type="text/plain; charset=utf-8",
        data=data,
        settings=_SETTINGS,
    )
    assert upload.extension == ".txt"
    assert upload.content_type == "text/plain"
    parsed = parse_document(data, extension=".txt", settings=_SETTINGS)
    assert parsed.page_count == 1
    assert "Hello world" in parsed.pages[0].text


def test_markdown_is_stored_as_markdown() -> None:
    upload = validate_upload(
        filename="notes.md",
        content_type="text/plain",
        data=b"# Heading\n\nBody",
        settings=_SETTINGS,
    )
    assert upload.content_type == "text/markdown"


def test_unsupported_and_invalid_uploads_are_rejected() -> None:
    with pytest.raises(UnsupportedDocumentError):
        validate_upload(
            filename="payload.exe",
            content_type="application/octet-stream",
            data=b"MZ",
            settings=_SETTINGS,
        )
    with pytest.raises(UnsupportedDocumentError):
        validate_upload(
            filename="fake.pdf",
            content_type="application/pdf",
            data=b"not a pdf",
            settings=_SETTINGS,
        )
    with pytest.raises(UnsupportedDocumentError):
        validate_upload(
            filename="binary.txt",
            content_type="text/plain",
            data=b"a\x00b",
            settings=_SETTINGS,
        )
    with pytest.raises(DocumentTooLargeError):
        validate_upload(
            filename="large.txt",
            content_type="text/plain",
            data=b"x" * 5000,
            settings=_SETTINGS,
        )


def test_invalid_utf8_and_empty_text_are_rejected() -> None:
    with pytest.raises(DocumentParseError):
        parse_document(b"\xff\xfe\x00", extension=".txt", settings=_SETTINGS)
    with pytest.raises(DocumentParseError):
        parse_document(b"   ", extension=".txt", settings=_SETTINGS)


def test_pdf_parsing_preserves_page_numbers() -> None:
    data = _pdf_bytes(pages=2)
    upload = validate_upload(
        filename="report.pdf",
        content_type="application/pdf",
        data=data,
        settings=_SETTINGS,
    )
    parsed = parse_document(data, extension=upload.extension, settings=_SETTINGS)
    assert parsed.page_count == 2
    assert [page.page_number for page in parsed.pages] == [1, 2]
    assert "Hello PDF page 1" in parsed.pages[0].text
    assert "Hello PDF page 2" in parsed.pages[1].text


def test_pdf_page_limit_is_enforced() -> None:
    settings = make_settings(
        document_max_bytes=4096,
        document_max_parsed_chars=5000,
        document_max_pages=1,
    )
    with pytest.raises(DocumentTooLargeError):
        parse_document(_pdf_bytes(pages=2), extension=".pdf", settings=settings)

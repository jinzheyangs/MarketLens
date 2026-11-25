"""不可信上传校验与按格式提取文本。"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from app.core.config import Settings
from app.rag.errors import (
    DocumentParseError,
    DocumentTooLargeError,
    UnsupportedDocumentError,
)
from app.rag.types import ParsedDocument, ParsedPage

_ALLOWED_TYPES: dict[str, frozenset[str]] = {
    ".txt": frozenset({"text/plain", "application/octet-stream"}),
    ".md": frozenset({"text/markdown", "text/plain", "application/octet-stream"}),
    ".markdown": frozenset({"text/markdown", "text/plain", "application/octet-stream"}),
    ".pdf": frozenset({"application/pdf", "application/octet-stream"}),
}


@dataclass(frozen=True, slots=True)
class UploadInfo:
    extension: str
    content_type: str
    filename: str


def sanitize_filename(filename: str) -> str:
    """Keep a display name only; object keys never use client-provided names."""
    name = Path(filename or "").name
    name = "".join(
        character for character in name if character >= " " and character != "\x7f"
    ).strip()
    if not name:
        name = "document"
    if len(name) > 255:
        suffix = Path(name).suffix[:20]
        stem_limit = max(1, 255 - len(suffix))
        name = f"{Path(name).stem[:stem_limit]}{suffix}"
    return name


def sanitize_title(title: str | None, filename: str) -> str:
    candidate = (title or "").strip() or Path(filename).stem
    candidate = "".join(
        character for character in candidate if character >= " " and character != "\x7f"
    ).strip()
    return (candidate or "Document")[:255]


def validate_upload(
    *,
    filename: str,
    content_type: str | None,
    data: bytes,
    settings: Settings,
) -> UploadInfo:
    if len(data) > settings.document_max_bytes:
        raise DocumentTooLargeError("upload exceeds DOCUMENT_MAX_BYTES")
    safe_name = sanitize_filename(filename)
    extension = Path(safe_name).suffix.lower()
    allowed_types = _ALLOWED_TYPES.get(extension)
    if allowed_types is None:
        raise UnsupportedDocumentError("extension is not allowlisted")

    normalized_type = (content_type or "application/octet-stream").split(";", 1)[0]
    normalized_type = normalized_type.strip().lower() or "application/octet-stream"
    if normalized_type not in allowed_types:
        raise UnsupportedDocumentError("content type does not match the extension")

    if extension == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise UnsupportedDocumentError("file does not have a PDF signature")
        stored_type = "application/pdf"
    else:
        if b"\x00" in data:
            raise UnsupportedDocumentError("text document contains binary data")
        stored_type = "text/markdown" if extension in {".md", ".markdown"} else "text/plain"

    return UploadInfo(extension=extension, content_type=stored_type, filename=safe_name)


def parse_document(data: bytes, *, extension: str, settings: Settings) -> ParsedDocument:
    if extension == ".pdf":
        return _parse_pdf(data, settings)
    return _parse_text(data, settings)


def _parse_text(data: bytes, settings: Settings) -> ParsedDocument:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentParseError("text is not valid UTF-8") from exc
    if "\x00" in text:
        raise UnsupportedDocumentError("text contains NUL bytes")
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise DocumentParseError("document has no text")
    if len(text) > settings.document_max_parsed_chars:
        raise DocumentTooLargeError("parsed text exceeds the configured limit")
    return ParsedDocument(pages=(ParsedPage(page_number=1, text=text),), page_count=1)


def _parse_pdf(data: bytes, settings: Settings) -> ParsedDocument:
    try:
        reader = PdfReader(BytesIO(data), strict=False)
    except Exception as exc:
        raise DocumentParseError("PDF structure is invalid") from exc
    if reader.is_encrypted:
        raise DocumentParseError("encrypted PDFs are not supported")

    page_count = len(reader.pages)
    if page_count > settings.document_max_pages:
        raise DocumentTooLargeError("PDF page count exceeds the configured limit")

    pages: list[ParsedPage] = []
    total_characters = 0
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:
            raise DocumentParseError(f"PDF page {index} could not be extracted") from exc
        text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
        total_characters += len(text)
        if total_characters > settings.document_max_parsed_chars:
            raise DocumentTooLargeError("parsed text exceeds the configured limit")
        pages.append(ParsedPage(page_number=index, text=text))

    if not any(page.text for page in pages):
        raise DocumentParseError("PDF contains no extractable text")
    return ParsedDocument(pages=tuple(pages), page_count=page_count)

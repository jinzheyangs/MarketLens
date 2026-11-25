"""安全的 HTML/纯文本提取，不执行页面内容。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol

from app.collector.errors import ExtractionError
from app.core.config import Settings

_SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "iframe", "object"}
_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "dl",
    "dt",
    "dd",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
_SPACE_PATTERN = re.compile(r"[ \t\f\v]+")
_NEWLINE_PATTERN = re.compile(r"\n{3,}")


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    title: str | None
    text: str
    truncated: bool


class ContentExtractor(Protocol):
    def extract(
        self,
        body: bytes,
        *,
        content_type: str,
        charset: str | None,
    ) -> ExtractedContent: ...


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if not self._skip_depth and tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        else:
            self.text_parts.append(data)


def _decode(body: bytes, charset: str | None) -> str:
    encoding = charset or "utf-8"
    try:
        text = body.decode(encoding, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _normalize(text: str) -> str:
    text = _SPACE_PATTERN.sub(" ", text)
    text = _NEWLINE_PATTERN.sub("\n\n", text)
    return text.strip()


class BasicContentExtractor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def extract(
        self,
        body: bytes,
        *,
        content_type: str,
        charset: str | None,
    ) -> ExtractedContent:
        decoded = _decode(body, charset)
        if content_type == "text/plain":
            text = _normalize(decoded)
            title = None
        elif content_type in {"text/html", "application/xhtml+xml"}:
            parser = _TextParser()
            try:
                parser.feed(decoded)
                parser.close()
            except Exception as exc:
                raise ExtractionError("HTML parsing failed") from exc
            title = _normalize("".join(parser.title_parts))[:500] or None
            text = _normalize("".join(parser.text_parts))
        else:
            raise ExtractionError("unsupported content type for extraction")

        if not text:
            raise ExtractionError("page has no extractable text")
        limit = self._settings.collector_max_extracted_chars
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]
        return ExtractedContent(title=title, text=text, truncated=truncated)


__all__ = ["BasicContentExtractor", "ContentExtractor", "ExtractedContent"]

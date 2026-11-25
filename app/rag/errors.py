"""不可信文档入库的安全错误类型。"""

from __future__ import annotations


class RagError(Exception):
    """Internal processing failure with a stable code safe for audit.

    ``str(exc)`` may contain internal details for server logs; ``safe_message``
    is the only text that may be shown to API clients.
    """

    code = "rag_error"
    safe_message = "The document could not be processed."

    def __init__(self, detail: str = "", *, code: str | None = None) -> None:
        super().__init__(detail or self.safe_message)
        if code is not None:
            self.code = code


class UnsupportedDocumentError(RagError):
    code = "unsupported_document_type"
    safe_message = "The document type is not supported."


class DocumentTooLargeError(RagError):
    code = "document_too_large"
    safe_message = "The document exceeds the configured limits."


class DocumentParseError(RagError):
    code = "document_parse_failed"
    safe_message = "The document could not be parsed."


class TooManyChunksError(RagError):
    code = "too_many_chunks"
    safe_message = "The document produces too many chunks."


class EmbeddingError(RagError):
    code = "embedding_failed"
    safe_message = "The document could not be embedded."


class VectorStoreError(RagError):
    code = "vector_store_failed"
    safe_message = "The vector index is unavailable."


class ObjectStorageError(RagError):
    code = "object_storage_failed"
    safe_message = "Object storage is unavailable."


class RagUnavailableError(RagError):
    code = "rag_unavailable"
    safe_message = "Document retrieval is not configured."

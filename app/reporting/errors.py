"""Safe reporting/review errors."""

from __future__ import annotations


class ReportingError(Exception):
    code = "reporting_error"
    safe_message = "The reporting operation failed."
    retryable = False

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.safe_message)


class ReviewProcessingError(ReportingError):
    code = "review_processing_failed"
    safe_message = "The evidence review failed."
    retryable = True


class ReportGenerationError(ReportingError):
    code = "report_generation_failed"
    safe_message = "The report could not be generated."
    retryable = False


class ExportProcessingError(ReportingError):
    code = "export_processing_failed"
    safe_message = "The report export failed."
    retryable = True


class ExportNotApprovedError(ReportingError):
    code = "export_not_approved"
    safe_message = "Only an approved report version can be exported."
    retryable = False


class ExportUnsupportedFormatError(ReportingError):
    code = "unsupported_export_format"
    safe_message = "The requested export format is not supported."
    retryable = False


__all__ = [
    "ExportNotApprovedError",
    "ExportProcessingError",
    "ExportUnsupportedFormatError",
    "ReportGenerationError",
    "ReportingError",
    "ReviewProcessingError",
]

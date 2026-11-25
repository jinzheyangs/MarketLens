"""采集器安全错误类型，带可重试分类。"""

from __future__ import annotations


class CollectorError(Exception):
    code = "collector_error"
    safe_message = "The source could not be collected."
    retryable = False

    def __init__(self, detail: str = "", *, status_code: int | None = None) -> None:
        super().__init__(detail or self.safe_message)
        self.status_code = status_code


class InvalidTargetError(CollectorError):
    code = "invalid_target"
    safe_message = "The target URL is invalid."


class BlockedTargetError(CollectorError):
    code = "blocked_target"
    safe_message = "The target is not permitted by the collection policy."


class DnsResolutionError(CollectorError):
    code = "dns_resolution_failed"
    safe_message = "The target host could not be resolved."
    retryable = True


class FetchTimeoutError(CollectorError):
    code = "fetch_timeout"
    safe_message = "The target did not respond in time."
    retryable = True


class FetchConnectionError(CollectorError):
    code = "fetch_connection_failed"
    safe_message = "The target could not be reached."
    retryable = True


class HttpStatusError(CollectorError):
    code = "http_status_error"
    safe_message = "The target returned an error status."

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}", status_code=status_code)
        self.retryable = status_code in {408, 425, 429} or status_code >= 500


class RedirectError(CollectorError):
    code = "redirect_rejected"
    safe_message = "The redirect chain was rejected."


class ResponseTooLargeError(CollectorError):
    code = "response_too_large"
    safe_message = "The response exceeds the configured size limit."


class UnsupportedContentTypeError(CollectorError):
    code = "unsupported_content_type"
    safe_message = "The response content type is not supported."


class UnsupportedContentEncodingError(CollectorError):
    code = "unsupported_content_encoding"
    safe_message = "Compressed responses are not accepted."


class RobotsDisallowedError(CollectorError):
    code = "robots_disallowed"
    safe_message = "The target disallows collection by this agent."


class RobotsUnavailableError(CollectorError):
    code = "robots_unavailable"
    safe_message = "The robots policy is temporarily unavailable."
    retryable = True


class ExtractionError(CollectorError):
    code = "extraction_failed"
    safe_message = "The page content could not be extracted."


class SearchProviderError(CollectorError):
    code = "search_provider_failed"
    safe_message = "The search provider is unavailable."
    retryable = True

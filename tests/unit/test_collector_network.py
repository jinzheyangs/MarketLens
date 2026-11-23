from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.collector.errors import (
    BlockedTargetError,
    DnsResolutionError,
    HttpStatusError,
    InvalidTargetError,
    RedirectError,
    ResponseTooLargeError,
    RobotsUnavailableError,
    UnsupportedContentEncodingError,
    UnsupportedContentTypeError,
)
from app.collector.network import (
    FetchResult,
    RobotsManager,
    SafeFetcher,
    is_public_address,
    parse_robots,
    resolve_addresses,
    validate_url,
)
from tests.conftest import make_settings


def _settings(**overrides):
    values = {
        "collector_allowed_ports": [80, 443],
        "collector_allowed_domains": [],
        "collector_blocked_domains": ["blocked.example"],
        "collector_allowed_content_types": ["text/html", "text/plain"],
        "collector_max_response_bytes": 1024,
        "collector_max_redirects": 2,
        "collector_request_timeout_seconds": 5,
        "collector_respect_robots": True,
        "collector_robots_cache_seconds": 60,
    }
    values.update(overrides)
    return make_settings(**values)


def test_url_validation_canonicalizes_and_removes_fragment() -> None:
    safe = validate_url(
        "https://Example.COM:443/path?q=1#fragment",
        _settings(),
    )
    assert safe.url == "https://example.com/path?q=1"
    assert safe.host == "example.com"
    assert safe.port == 443

    http_safe = validate_url("http://example.com", _settings())
    assert http_safe.url == "http://example.com/"


def test_url_validation_rejects_unsafe_targets() -> None:
    settings = _settings(collector_allowed_ports=[80, 443, 8080])
    for raw in (
        "file:///etc/passwd",
        "gopher://example.com/",
        "https://user:pass@example.com/",
        "https://example.com:22/",
        "https://blocked.example/",
        "https://example.com/a b",
        "https://example.com\\evil",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
    ):
        with pytest.raises((InvalidTargetError, BlockedTargetError)):
            validate_url(raw, settings)


def test_domain_policy_allows_subdomains_and_blocks_others() -> None:
    settings = _settings(
        collector_allowed_domains=["example.com"],
        collector_blocked_domains=["private.example.com"],
    )
    assert validate_url("https://sub.example.com/", settings).host == "sub.example.com"
    with pytest.raises(BlockedTargetError):
        validate_url("https://other.example.org/", settings)
    with pytest.raises(BlockedTargetError):
        validate_url("https://private.example.com/", settings)


def test_ip_address_boundaries() -> None:
    for value in (
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "100.64.0.1",
        "169.254.169.254",
        "198.18.0.1",
        "0.0.0.0",  # noqa: S104
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
    ):
        assert is_public_address(value) is False, value
    assert is_public_address("8.8.8.8") is True
    assert is_public_address("2606:4700:4700::1111") is True


def test_dns_resolution_fails_closed_on_private_or_mixed_answers() -> None:
    safe = validate_url("https://example.com/", _settings())
    public = ["93.184.216.34"]
    assert resolve_addresses(safe, _settings(), resolver=lambda _h, _p: public) == public

    with pytest.raises(BlockedTargetError):
        resolve_addresses(safe, _settings(), resolver=lambda _h, _p: ["10.0.0.1"])
    with pytest.raises(BlockedTargetError):
        resolve_addresses(
            safe,
            _settings(),
            resolver=lambda _h, _p: ["93.184.216.34", "127.0.0.1"],
        )
    with pytest.raises(DnsResolutionError):
        resolve_addresses(safe, _settings(), resolver=lambda _h, _p: [])

    permissive = _settings(collector_allow_private_networks=True)
    assert resolve_addresses(safe, permissive, resolver=lambda _h, _p: ["10.0.0.1"]) == ["10.0.0.1"]


def test_robots_parser_honors_specific_agent_allow_and_crawl_delay() -> None:
    text = """
    User-agent: *
    Disallow: /private
    Crawl-delay: 5

    User-agent: marketlenscollector
    Allow: /private/public
    Disallow: /private
    Crawl-delay: 2
    """
    policy = parse_robots(text, "MarketLensCollector/0.1")
    assert policy.allows("/") is True
    assert policy.allows("/private/page") is False
    assert policy.allows("/private/public/page") is True
    assert policy.crawl_delay == 2.0


def test_robots_manager_caches_and_fails_closed() -> None:
    class FakeFetcher:
        def __init__(self) -> None:
            self.calls = 0

        async def fetch(self, url: str, *, content_types=None, max_bytes=None):
            self.calls += 1
            return FetchResult(
                requested_url=url,
                final_url=url,
                status_code=200,
                content_type="text/plain",
                charset=None,
                body=b"User-agent: *\nDisallow: /secret\n",
                content_sha256="hash",
                redirects=(),
            )

    settings = _settings()
    fetcher = FakeFetcher()
    manager = RobotsManager(settings, fetcher)  # type: ignore[arg-type]
    safe = validate_url("https://example.com/secret/page", settings)

    async def scenario():
        first = await manager.policy_for(safe)
        second = await manager.policy_for(safe)
        assert first.allows("/secret/page") is False
        assert second.allows("/secret/page") is False
        assert fetcher.calls == 1

    import asyncio

    asyncio.run(scenario())


def test_robots_manager_maps_http_statuses() -> None:
    class StatusFetcher:
        def __init__(self, error: Exception) -> None:
            self.error = error

        async def fetch(self, url: str, *, content_types=None, max_bytes=None):
            raise self.error

    settings = _settings()
    safe = validate_url("https://example.com/", settings)

    import asyncio

    allow_manager = RobotsManager(
        settings,
        StatusFetcher(HttpStatusError(404)),  # type: ignore[arg-type]
    )
    assert asyncio.run(allow_manager.policy_for(safe)).allows("/") is True

    block_manager = RobotsManager(
        settings,
        StatusFetcher(HttpStatusError(403)),  # type: ignore[arg-type]
    )
    assert asyncio.run(block_manager.policy_for(safe)).allows("/") is False

    with pytest.raises(RobotsUnavailableError):
        asyncio.run(
            RobotsManager(
                settings,
                StatusFetcher(HttpStatusError(503)),  # type: ignore[arg-type]
            ).policy_for(safe)
        )


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args) -> None:
        return None

    def do_GET(self) -> None:
        if self.path == "/page":
            body = b"<html><head><title>Example</title></head><body><p>Hello</p></body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/loop":
            self.send_response(302)
            self.send_header("Location", "/loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/large":
            body = b"x" * 4096
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/json":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/gzip":
            body = b"compressed"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


@pytest.fixture
def local_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _local_settings(port: int):
    return _settings(
        collector_allow_private_networks=True,
        collector_allowed_ports=[port],
        collector_allowed_domains=[],
        collector_allowed_content_types=["text/html", "text/plain"],
    )


def test_safe_fetcher_follows_redirect_and_returns_content(local_server: int) -> None:
    import asyncio

    fetcher = SafeFetcher(_local_settings(local_server))
    result = asyncio.run(fetcher.fetch(f"http://127.0.0.1:{local_server}/redirect"))
    assert result.status_code == 200
    assert result.final_url.endswith("/page")
    assert result.redirects == ("/page",)
    assert "Hello" in result.body.decode()


def test_safe_fetcher_enforces_redirect_content_and_size_limits(local_server: int) -> None:
    import asyncio

    fetcher = SafeFetcher(_local_settings(local_server))
    with pytest.raises(RedirectError):
        asyncio.run(fetcher.fetch(f"http://127.0.0.1:{local_server}/loop"))
    with pytest.raises(ResponseTooLargeError):
        asyncio.run(fetcher.fetch(f"http://127.0.0.1:{local_server}/large"))
    with pytest.raises(UnsupportedContentTypeError):
        asyncio.run(fetcher.fetch(f"http://127.0.0.1:{local_server}/json"))
    with pytest.raises(UnsupportedContentEncodingError):
        asyncio.run(fetcher.fetch(f"http://127.0.0.1:{local_server}/gzip"))


def test_safe_fetcher_blocks_private_targets_by_default(local_server: int) -> None:
    settings = _settings(
        collector_allow_private_networks=False,
        collector_allowed_ports=[local_server],
    )
    with pytest.raises(BlockedTargetError):
        validate_url(f"http://127.0.0.1:{local_server}/page", settings)

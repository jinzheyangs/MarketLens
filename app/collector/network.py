"""防 SSRF 的 URL 校验、DNS 固定、HTTP 抓取与 robots 策略。

The fetcher deliberately does not use a general HTTP client that re-resolves DNS
after validation. It resolves once, rejects any non-public answer, and connects
to the validated IP while keeping the original hostname for Host/TLS SNI and
certificate verification. Redirects repeat the full validation.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from app.collector.errors import (
    BlockedTargetError,
    DnsResolutionError,
    FetchConnectionError,
    FetchTimeoutError,
    HttpStatusError,
    InvalidTargetError,
    RedirectError,
    ResponseTooLargeError,
    RobotsUnavailableError,
    UnsupportedContentEncodingError,
    UnsupportedContentTypeError,
)
from app.core.config import Settings

Resolver = Callable[[str, int], Sequence[str]]

_MAX_URL_LENGTH = 2048
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_BLOCKED_V4 = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
    )
)
_BLOCKED_V6 = tuple(
    ipaddress.ip_network(network)
    for network in (
        "::/128",
        "::1/128",
        "::/96",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "100::/64",
        "2001::/32",
        "2001:2::/48",
        "2001:10::/28",
        "2001:20::/28",
        "2001:db8::/32",
        "2002::/16",
        "3fff::/20",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


@dataclass(frozen=True, slots=True)
class SafeUrl:
    scheme: str
    host: str
    port: int
    path: str
    query: str

    @property
    def netloc(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default_port = 443 if self.scheme == "https" else 80
        return host if self.port == default_port else f"{host}:{self.port}"

    @property
    def url(self) -> str:
        path = self.path or "/"
        return urlunsplit((self.scheme, self.netloc, path, self.query, ""))

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.netloc}"

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()

    @property
    def request_target(self) -> str:
        path = quote(self.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if self.query:
            path += "?" + quote(self.query, safe="=&%:@!$'()*+,;/?-._~")
        return path


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    charset: str | None
    body: bytes
    content_sha256: str
    redirects: tuple[str, ...]


def _host_matches(host: str, entry: str) -> bool:
    return host == entry or host.endswith(f".{entry}")


def _enforce_domain_policy(host: str, settings: Settings) -> None:
    if any(_host_matches(host, entry) for entry in settings.collector_blocked_domains):
        raise BlockedTargetError("host is blocked by domain policy")
    allowed = settings.collector_allowed_domains
    if allowed and not any(_host_matches(host, entry) for entry in allowed):
        raise BlockedTargetError("host is not in the allowed domain policy")


def _normalize_host(host: str) -> str:
    candidate = host.rstrip(".").lower()
    if not candidate or "%" in candidate:
        raise InvalidTargetError("host is empty or contains a zone identifier")
    try:
        ipaddress.ip_address(candidate)
        return candidate
    except ValueError:
        pass
    try:
        ascii_host = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidTargetError("host is not a valid DNS name") from exc
    if len(ascii_host) > 253:
        raise InvalidTargetError("host is too long")
    return ascii_host


def validate_url(raw: str, settings: Settings) -> SafeUrl:
    candidate = (raw or "").strip()
    if not candidate or len(candidate) > _MAX_URL_LENGTH:
        raise InvalidTargetError("URL is empty or too long")
    if "\\" in candidate or any(
        ord(character) < 0x21 or ord(character) == 0x7F for character in candidate
    ):
        raise InvalidTargetError("URL contains whitespace or control characters")

    try:
        parsed = urlsplit(candidate)
    except ValueError as exc:
        raise InvalidTargetError("URL could not be parsed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise InvalidTargetError("only http/https targets are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise InvalidTargetError("URLs with credentials are not allowed")
    if not parsed.hostname:
        raise InvalidTargetError("URL has no host")

    host = _normalize_host(parsed.hostname)
    try:
        explicit_port = parsed.port
    except ValueError as exc:
        raise InvalidTargetError("URL has an invalid port") from exc
    port = explicit_port or (443 if scheme == "https" else 80)
    if port not in settings.collector_allowed_ports:
        raise BlockedTargetError("target port is not allowed")
    _enforce_domain_policy(host, settings)
    literal = _parse_ip(host)
    if (
        literal is not None
        and not settings.collector_allow_private_networks
        and not is_public_address(host)
    ):
        raise BlockedTargetError("IP literal is not a public address")

    safe = SafeUrl(
        scheme=scheme,
        host=host,
        port=port,
        path=parsed.path or "/",
        query=parsed.query,
    )
    if len(safe.url) > _MAX_URL_LENGTH:
        raise InvalidTargetError("canonical URL is too long")
    return safe


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = value.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def is_public_address(value: str) -> bool:
    address = _parse_ip(value)
    if address is None:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        if not address.is_global:
            return False
        return not any(address in network for network in _BLOCKED_V4)
    if (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
    ):
        return False
    if not address.is_global:
        return False
    return not any(address in network for network in _BLOCKED_V6)


def _default_resolver(host: str, port: int) -> Sequence[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise DnsResolutionError("DNS resolution failed") from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def resolve_addresses(
    safe: SafeUrl,
    settings: Settings,
    *,
    resolver: Resolver | None = None,
) -> list[str]:
    address_literal = _parse_ip(safe.host)
    if address_literal is not None:
        addresses = [safe.host]
    else:
        try:
            addresses = list((resolver or _default_resolver)(safe.host, safe.port))
        except DnsResolutionError:
            raise
        except OSError as exc:
            raise DnsResolutionError("DNS resolution failed") from exc
    addresses = list(dict.fromkeys(addresses))
    if not addresses:
        raise DnsResolutionError("host has no addresses")
    for address in addresses:
        if _parse_ip(address) is None:
            raise DnsResolutionError("resolver returned an invalid address")
        if not settings.collector_allow_private_networks and not is_public_address(address):
            raise BlockedTargetError("target resolves to a non-public address")
    return addresses


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        address: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._ssl_context = context

    def connect(self) -> None:
        raw_socket = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._ssl_context.wrap_socket(raw_socket, server_hostname=self.host)


class SafeFetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        resolver: Resolver | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._ssl_context = ssl.create_default_context()

    async def fetch(
        self,
        url: str,
        *,
        content_types: set[str] | None = None,
        max_bytes: int | None = None,
    ) -> FetchResult:
        allowed_types = {
            value.split(";", 1)[0].strip().lower()
            for value in (content_types or self._settings.collector_allowed_content_types)
        }
        limit = max_bytes or self._settings.collector_max_response_bytes
        try:
            async with asyncio.timeout(self._settings.collector_request_timeout_seconds + 1):
                return await asyncio.to_thread(
                    self._fetch_sync,
                    url,
                    allowed_types,
                    limit,
                )
        except TimeoutError as exc:
            raise FetchTimeoutError("fetch deadline exceeded") from exc

    def _fetch_sync(
        self,
        url: str,
        allowed_types: set[str],
        max_bytes: int,
    ) -> FetchResult:
        deadline = time.monotonic() + self._settings.collector_request_timeout_seconds
        current_url = url
        redirects: list[str] = []
        seen: set[str] = set()

        while True:
            safe = validate_url(current_url, self._settings)
            if safe.url in seen:
                raise RedirectError("redirect loop detected")
            seen.add(safe.url)

            addresses = resolve_addresses(
                safe,
                self._settings,
                resolver=self._resolver,
            )
            response: http.client.HTTPResponse | None = None
            connection: http.client.HTTPConnection | None = None
            last_error: Exception | None = None

            for address in addresses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FetchTimeoutError("fetch deadline exceeded")
                try:
                    connection = self._connect(safe, address, remaining)
                    connection.connect()
                    if connection.sock is not None:
                        connection.sock.settimeout(max(0.1, remaining))
                    connection.request(
                        "GET",
                        safe.request_target,
                        headers={
                            "User-Agent": self._settings.collector_user_agent,
                            "Accept": ", ".join(sorted(allowed_types)),
                            "Accept-Encoding": "identity",
                            "Connection": "close",
                        },
                    )
                    response = connection.getresponse()
                    break
                except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    last_error = exc
                    if connection is not None:
                        connection.close()
                    connection = None

            if response is None:
                raise FetchConnectionError("all resolved addresses failed") from last_error

            try:
                status = response.status
                if status in _REDIRECT_STATUSES:
                    location = response.getheader("location")
                    if not location:
                        raise RedirectError("redirect has no Location header")
                    if len(redirects) >= self._settings.collector_max_redirects:
                        raise RedirectError("redirect limit exceeded")
                    next_url = urljoin(safe.url, location)
                    next_safe = validate_url(next_url, self._settings)
                    if safe.scheme == "https" and next_safe.scheme != "https":
                        raise RedirectError("https-to-http redirect is not allowed")
                    redirects.append(location)
                    current_url = next_safe.url
                    continue

                if not 200 <= status < 300:
                    raise HttpStatusError(status)

                content_type_header = response.getheader("content-type")
                if not content_type_header:
                    raise UnsupportedContentTypeError("missing content type")
                parts = [part.strip() for part in content_type_header.split(";")]
                content_type = parts[0].lower()
                if content_type not in allowed_types:
                    raise UnsupportedContentTypeError(f"content type {content_type} is not allowed")
                charset: str | None = None
                for parameter in parts[1:]:
                    if parameter.lower().startswith("charset="):
                        charset = parameter.split("=", 1)[1].strip("\"'") or None

                content_encoding = (response.getheader("content-encoding") or "").strip()
                if content_encoding and content_encoding.lower() != "identity":
                    raise UnsupportedContentEncodingError("compressed responses are not accepted")

                declared_length = response.getheader("content-length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > max_bytes:
                            raise ResponseTooLargeError("content-length exceeds limit")
                    except ValueError:
                        pass

                try:
                    body = response.read(max_bytes + 1)
                except (OSError, http.client.HTTPException) as exc:
                    raise FetchConnectionError("response read failed") from exc
                if len(body) > max_bytes:
                    raise ResponseTooLargeError("response body exceeds limit")

                return FetchResult(
                    requested_url=url,
                    final_url=safe.url,
                    status_code=status,
                    content_type=content_type,
                    charset=charset,
                    body=body,
                    content_sha256=hashlib.sha256(body).hexdigest(),
                    redirects=tuple(redirects),
                )
            finally:
                response.close()
                if connection is not None:
                    connection.close()

    def _connect(
        self,
        safe: SafeUrl,
        address: str,
        timeout: float,
    ) -> http.client.HTTPConnection:
        if safe.scheme == "https":
            return _PinnedHTTPSConnection(
                safe.host,
                safe.port,
                address,
                timeout,
                self._ssl_context,
            )
        return _PinnedHTTPConnection(safe.host, safe.port, address, timeout)


@dataclass(frozen=True, slots=True)
class RobotsRule:
    pattern: str
    allow: bool


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    rules: tuple[RobotsRule, ...] = ()
    crawl_delay: float | None = None
    disallow_all: bool = False

    @classmethod
    def allow_all(cls) -> RobotsPolicy:
        return cls()

    @classmethod
    def block_all(cls) -> RobotsPolicy:
        return cls(disallow_all=True)

    def allows(self, path: str) -> bool:
        if self.disallow_all:
            return False
        matching = [rule for rule in self.rules if _rule_matches(rule.pattern, path)]
        if not matching:
            return True
        best_length = max(len(rule.pattern) for rule in matching)
        best = [rule for rule in matching if len(rule.pattern) == best_length]
        return any(rule.allow for rule in best)


def _rule_matches(pattern: str, path: str) -> bool:
    if not pattern:
        return False
    anchored = pattern.endswith("$")
    raw = pattern[:-1] if anchored else pattern
    expression = "".join(".*" if char == "*" else re.escape(char) for char in raw)
    if anchored:
        expression += "$"
    return re.match(expression, path) is not None


def _product_token(user_agent: str) -> str:
    token = user_agent.split(" ", 1)[0].split("/", 1)[0]
    return token.strip().lower() or "marketscopecollector"


@dataclass(slots=True)
class _RobotsGroup:
    agents: list[str]
    rules: list[RobotsRule]
    delay: float | None = None


def parse_robots(text: str, user_agent: str) -> RobotsPolicy:
    groups: list[_RobotsGroup] = []
    current: _RobotsGroup | None = None
    current_has_rules = False

    for raw_line in text.replace("\x00", "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if current is None or current_has_rules:
                current = _RobotsGroup(agents=[], rules=[])
                groups.append(current)
                current_has_rules = False
            current.agents.append(value.lower())
        elif key in {"allow", "disallow"} and current is not None and value:
            current.rules.append(RobotsRule(pattern=value, allow=key == "allow"))
            current_has_rules = True
        elif key == "crawl-delay" and current is not None:
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay >= 0:
                current.delay = delay
                current_has_rules = True

    token = _product_token(user_agent)
    exact = [
        group
        for group in groups
        if any(
            agent == token or agent.startswith(f"{token}/") or agent.startswith(f"{token} ")
            for agent in group.agents
        )
    ]
    selected = exact or [group for group in groups if "*" in group.agents]

    rules: list[RobotsRule] = []
    delays: list[float] = []
    for group in selected:
        rules.extend(group.rules)
        if group.delay is not None:
            delays.append(group.delay)
    return RobotsPolicy(
        rules=tuple(rules),
        crawl_delay=max(delays) if delays else None,
    )


class RobotsManager:
    def __init__(self, settings: Settings, fetcher: SafeFetcher) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._cache: dict[str, tuple[RobotsPolicy, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def policy_for(self, safe: SafeUrl) -> RobotsPolicy:
        if not self._settings.collector_respect_robots:
            return RobotsPolicy.allow_all()
        origin = safe.origin
        now = time.monotonic()
        cached = self._cache.get(origin)
        if cached is not None and cached[1] > now:
            return cached[0]

        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            cached = self._cache.get(origin)
            if cached is not None and cached[1] > time.monotonic():
                return cached[0]

            robots_url = urlunsplit((safe.scheme, safe.netloc, "/robots.txt", "", ""))
            try:
                result = await self._fetcher.fetch(
                    robots_url,
                    content_types={"text/plain", "text/html"},
                    max_bytes=self._settings.collector_robots_max_bytes,
                )
            except HttpStatusError as exc:
                status = exc.status_code or 0
                if status in {401, 403}:
                    policy = RobotsPolicy.block_all()
                elif status in {429} or status >= 500:
                    raise RobotsUnavailableError("robots endpoint unavailable") from exc
                else:
                    policy = RobotsPolicy.allow_all()
            except (FetchTimeoutError, FetchConnectionError, DnsResolutionError) as exc:
                raise RobotsUnavailableError("robots endpoint unavailable") from exc

            else:
                policy = parse_robots(
                    result.body.decode("utf-8", errors="replace"),
                    self._settings.collector_user_agent,
                )

            ttl = self._settings.collector_robots_cache_seconds
            if ttl > 0:
                self._cache[origin] = (policy, time.monotonic() + ttl)
            return policy


__all__ = [
    "FetchResult",
    "RobotsManager",
    "RobotsPolicy",
    "SafeFetcher",
    "SafeUrl",
    "is_public_address",
    "parse_robots",
    "resolve_addresses",
    "validate_url",
]

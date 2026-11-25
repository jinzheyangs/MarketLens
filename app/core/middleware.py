"""ASGI 中间件：请求关联、限制、安全响应头与访问日志。

中间件顺序（由外到内）在 ``app.factory`` 中配置：

    RequestContext -> AccessLog -> SecurityHeaders -> CORS -> UnhandledError
    -> Timeout -> BodySizeLimit -> TrustedHost -> application
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.context import (
    is_safe_request_id,
    reset_request_id,
    set_request_id,
)
from app.core.errors import ErrorCode, RequestEntityTooLargeError
from app.core.responses import error_response

logger = logging.getLogger(__name__)


class RequestContextMiddleware:
    """分配安全的请求 ID，并暴露到响应头与日志中。"""

    def __init__(self, app: ASGIApp, *, header_name: str) -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        #  - scope：请求信息（url、请求头是什么）  - receive：读取用户发过来的数据     - send：发给用户返回内容
        if scope["type"] != "http":     #判断：是不是普通 http 请求（网页、接口请求）
            await self.app(scope, receive, send)
            return

        # 优先沿用入站请求带来的请求 ID（须通过安全字符校验），否则生成新的
        inbound = Headers(scope=scope).get(self.header_name)
        request_id = inbound if inbound and is_safe_request_id(inbound) else uuid4().hex    # 优先沿用入站请求带来的请求 ID（须通过安全字符校验），否则生成新的

        state = scope.setdefault("state", {})       #请求自带一个 “小储物盒 state”，没有就新建一个空字典。
        if isinstance(state, dict):
            state["request_id"] = request_id        #把编号放进这个请求的储物盒，接口代码可以读取：`request.state.request_id`

        token = set_request_id(request_id)          #把编号放到一个【全局看不见的小标签】上，**日志打印的时候自动带上这个编号**，不用你每个地方手动传。

        async def send_wrapper(message: Message) -> None:
            # 响应起始帧把请求 ID 回写到响应头
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers[self.header_name] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_request_id(token)     #写到content var


class AccessLogMiddleware:
    """每个请求输出一条结构化日志，不含查询串与请求头。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":         #如果不是普通 HTTP 请求（websocket、lifespan 启动信号），直接放行，**不打访问日志**。
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()       #记下**请求开始的时间点**（高精度计时器，适合算耗时）
        status_code = 500       #给状态码一个默认值 500。

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code        #代表这个函数要修改外层的 `status_code` 变量
            if message["type"] == "http.response.start":    #判断：这条消息是不是【响应头包】`http.response.start`
                status_code = int(message["status"])    #如果是：从里面拿出状态码，保存到外层变量 `status_code`
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = str(scope.get("path", ""))   #拿到用户真实访问的路径，拿不到就给空字符串
            route = scope.get("route")      #`route` 是 FastAPI 识别出来的路由对象
            route_path = str(getattr(route, "path", path))  #如果 route 存在，就读 `route.path` → 拿到**路由模板**：`/api/user/{id}`
            duration_ms = round((time.perf_counter() - started) * 1000, 2)  #算出这个接口耗时多少毫秒
            client = scope.get("client")    #拿到(客户端IP, 端口)
            client_host = client[0] if isinstance(client, tuple) and client else None   #拿到IP
            context: dict[str, Any] = {
                "method": scope.get("method"),
                "path": route_path,
                "status": status_code,
                "duration_ms": duration_ms,
            }   #组装一个字典，放日志核心信息：
            state = scope.get("state")
            if isinstance(state, dict) and state.get("tenant_id") is not None:
                context["tenant_id"] = state["tenant_id"]
            if client_host is not None:
                context["client"] = client_host
            # 按状态码选择日志级别：健康检查降级为 DEBUG/WARNING
            if path.startswith("/health/"):
                level = logging.DEBUG if status_code < 400 else logging.WARNING
            elif status_code >= 500:
                level = logging.ERROR
            elif status_code >= 400:
                level = logging.WARNING
            else:
                level = logging.INFO
            logger.log(level, "http_request", extra={"context": context})   #进行写日志


class UnhandledErrorMiddleware:
    """把包裹栈内未预期的异常转换为 500 统一错误信封。

    放在关联/安全/CORS 包装层之内，可保证即便出现意外失败，响应仍能带上
    请求 ID、安全头与 CORS 头；Starlette 外层处理器仅作为最后兜底。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":     #非 HTTP 请求（websocket）直接放行，不做异常捕获。
            await self.app(scope, receive, send)
            return

        response_started = False        #**有没有已经发出响应头包裹（`http.response.start`）**

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":    #只要下游业务发出了【响应头包裹】，立刻把 `response_started` 标记改成 True。
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            logger.error(
                "unhandled_exception",
                extra={
                    "context": {
                        "method": scope.get("method"),
                        "path": scope.get("path"),
                    }
                },
                exc_info=(type(exc), exc, exc.__traceback__),   #抓到异常，打印日志
            )
            # 响应一旦已经开始就无法再改写状态码/信封，只能记录日志
            if response_started:
                logger.error(
                    "unhandled_exception_after_response_started",
                    extra={"context": {"path": scope.get("path")}},
                )
                return
            response = error_response(
                500,
                ErrorCode.INTERNAL_ERROR,
                "An internal error occurred.",
            )
            await response(scope, receive, send)    #执行一个 ASGI 响应对象


def parse_host_header(host_header: str | None) -> str | None:
    """从不可信的 Host 头中提取归一化主机名。

    格式非法时返回 ``None``；IPv6 字面量保留方括号；不做 DNS/IDNA 解析或网络访问。
    """
    if host_header is None:     #没传 Host 头 → 直接返回空。
        return None
    value = host_header.strip()
    if not value or any(character in value for character in " \t\r\n/\\@"): #如果变空，或者包含这些字符：`空格、制表符、换行、/ \ @` → 非法，返回 None
        return None

    if value.startswith("["):
        closing = value.find("]")
        if closing == -1:
            return None
        host = value[: closing + 1]
        remainder = value[closing + 1 :]
        if remainder and not (remainder.startswith(":") and remainder[1:].isdigit()):
            return None
        return host.lower()

    if value.count(":") > 1:
        return None
    host, separator, port = value.partition(":")
    if not host or (separator and not port.isdigit()):
        return None
    return host.lower()


class TrustedHostMiddleware:
    """主机名白名单校验，输出统一 JSON 错误信封。

    兼容 Starlette 的通配符语义（``*.example.com``），但不做 ``www`` 跳转；
    API 主机名必须精确匹配。
    """

    def __init__(self, app: ASGIApp, *, allowed_hosts: Sequence[str]) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")     #如果没有允许的 则抛错误
        for pattern in allowed_hosts:   #逐一进行遍历域名
            if "*" in pattern[1:]:      #星号**只能写在最开头**，不能出现在中间、末尾。
                raise ValueError(f"Invalid host wildcard pattern: {pattern}")
            if pattern.startswith("*") and pattern != "*" and not pattern.startswith("*."): #只允许：`*.example.com` 或者单独一个 `*`
                raise ValueError(f"Invalid host wildcard pattern: {pattern}")
        self.app = app
        self.allowed_hosts = [pattern.lower() for pattern in allowed_hosts]    # 全部转小写，域名大小写无关，统一比较。
        self.allow_any = "*" in self.allowed_hosts              #标记是否开启 “不校验、全部放行”。

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.allow_any or scope["type"] != "http":       #不是 HTTP 请求（websocket），不做 Host 校验，直接放行
            await self.app(scope, receive, send)
            return

        host = parse_host_header(Headers(scope=scope).get("host"))  #从 scope 取出 `Host` 请求头
        if host is not None:
            allowed = any(
                host == pattern or (pattern.startswith("*") and host.endswith(pattern[1:]))
                for pattern in self.allowed_hosts
            )#判断能不能通过
            if allowed:
                await self.app(scope, receive, send)    #如果通过则执行下列的逻辑
                return

        response = error_response(
            400,
            ErrorCode.BAD_REQUEST,
            "The host header is not permitted.",
        )
        await response(scope, receive, send)        #直接发出一套 ASGI 响应，不跑业务。


class SecurityHeadersMiddleware:
    """为 JSON API 注入保守的安全响应头。"""

    def __init__(self, app: ASGIApp, *, enable_hsts: bool) -> None:
        self.app = app
        self.enable_hsts = enable_hsts      #**要不要加上 HSTS 头**

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":     #不是 HTTP 请求（websocket、lifespan）直接放行，不加安全头。
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":        #只在【准备发送响应头】这一步处理，响应体消息直接跳过。
                headers = MutableHeaders(scope=message)         #把响应头变成可以修改的对象。
                defaults = {
                    "X-Content-Type-Options": "nosniff",
                    "X-Frame-Options": "DENY",
                    "Referrer-Policy": "no-referrer",
                    "Cross-Origin-Opener-Policy": "same-origin",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                    "Cache-Control": "no-store",
                }       #把响应头变成可以修改的对象。
                for key, value in defaults.items():
                    if key not in headers:
                        headers[key] = value
                # 仅生产环境开启 HSTS
                if self.enable_hsts and "Strict-Transport-Security" not in headers:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RequestTimeoutMiddleware:
    """限制单个请求的总处理时长。"""

    def __init__(self, app: ASGIApp, *, timeout_seconds: float) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds      #超时阈值，比如传 `10` 代表这个请求最多允许跑 10 秒

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False    #记录**有没有已经开始发送响应**（有没有发出 `http.response.start` 包裹）。

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            async with asyncio.timeout(self.timeout_seconds):       #从进入这一行开始计时
                await self.app(scope, receive, send_wrapper)
        except TimeoutError:
            # 响应已开始则无法再改状态码，仅记录日志
            if response_started:
                logger.error(
                    "request_timeout_after_response_started",
                    extra={"context": {"path": scope.get("path")}},
                )
                return
            response = error_response(
                504,
                ErrorCode.REQUEST_TIMEOUT,
                "The request exceeded the permitted processing time.",
                retryable=True,
            )
            await response(scope, receive, send)


class BodySizeLimitMiddleware:
    """在读取过程中拒绝超大请求体。

    先校验 ``Content-Length``；分块/流式请求在到达时逐段计数，超限抛出
    ``AppError``，由常规异常处理器转换为统一的 413 信封。
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes        #最大允许字节数

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 先按声明的 Content-Length 做前置拦截
        declared_length = Headers(scope=scope).get("content-length")    #获取Content-Length（快速预检）
        if declared_length is not None:
            try:
                declared_bytes = int(declared_length)
            except ValueError:
                declared_bytes = None
            if declared_bytes is not None and declared_bytes > self.max_body_bytes:
                response = error_response(
                    413,
                    ErrorCode.PAYLOAD_TOO_LARGE,
                    "The request body exceeds the permitted size.",
                )
                await response(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise RequestEntityTooLargeError
            return message

        await self.app(scope, limited_receive, send)

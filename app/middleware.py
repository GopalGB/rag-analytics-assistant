"""HTTP hardening: body-size limit, per-IP rate limiting, optional API key, security headers."""

from __future__ import annotations

import time

from starlette.responses import JSONResponse

PUBLIC_PREFIXES = ("/health", "/ui", "/static")
PUBLIC_EXACT = {"/", "/favicon.ico"}


_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
)


def _client_ip(scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def _header(scope, name: bytes) -> bytes | None:
    return next((value for key, value in scope.get("headers", []) if key.lower() == name), None)


class SecurityHeadersMiddleware:
    """Outermost ASGI wrapper that adds headers to every HTTP response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def send_with_security_headers(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                existing = {name.lower() for name, _ in message.get("headers", [])}
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        *(header for header in _SECURITY_HEADERS if header[0] not in existing),
                    ],
                }
            await send(message)

        try:
            await self.app(scope, receive, send_with_security_headers)
        except Exception:
            if response_started:
                raise
            response = JSONResponse({"error": "internal server error"}, status_code=500)
            await response(scope, receive, send_with_security_headers)


class BodyLimitMiddleware:
    """Cap actual ASGI request bytes and replay a valid body downstream."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body = bytearray()
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    await self.app(scope, _disconnected_receive, send)
                    return
                if message["type"] != "http.request":
                    await self.app(scope, receive, send)
                    return
                chunk = message.get("body", b"")
                body.extend(chunk)
                if len(body) > self.max_bytes:
                    await JSONResponse({"error": "request body too large"}, status_code=413)(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
        except Exception:
            await JSONResponse({"error": "invalid request body"}, status_code=400)(scope, receive, send)
            return

        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, receive, send)


async def _disconnected_receive():
    return {"type": "http.disconnect"}


class RateLimitMiddleware:
    """Simple per-IP token bucket; only meters mutating (POST) requests."""

    def __init__(self, app, per_minute: int, burst: int):
        self.app = app
        self.rate = per_minute / 60.0
        self.burst = burst
        self._tokens: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._max_clients = 4096
        self._ttl = 300.0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        ip = _client_ip(scope)
        now = time.monotonic()
        if len(self._last) >= self._max_clients:
            expired = [key for key, stamp in self._last.items() if now - stamp > self._ttl]
            for key in expired:
                self._last.pop(key, None)
                self._tokens.pop(key, None)
            if len(self._last) >= self._max_clients:
                oldest = min(self._last, key=self._last.get)
                self._last.pop(oldest, None)
                self._tokens.pop(oldest, None)
        previous = self._last.get(ip, now)
        tokens = self._tokens.get(ip, float(self.burst))
        self._tokens[ip] = min(self.burst, tokens + (now - previous) * self.rate)
        self._last[ip] = now
        if self._tokens[ip] < 1.0:
            await JSONResponse({"error": "rate limit exceeded"}, status_code=429)(scope, receive, send)
            return
        self._tokens[ip] -= 1.0
        await self.app(scope, receive, send)


class ApiKeyMiddleware:
    """If an API key is configured, require it on non-public paths (loopback is exempt)."""

    def __init__(self, app, api_key: str, trust_loopback: bool = True):
        self.app = app
        self.api_key = api_key
        self.trust_loopback = trust_loopback

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path in PUBLIC_EXACT or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            await self.app(scope, receive, send)
            return
        # Loopback exemption is opt-in: disable it behind a reverse proxy, where every request
        # would otherwise appear to originate from 127.0.0.1 and bypass the key.
        if self.trust_loopback and _client_ip(scope) in ("127.0.0.1", "::1", "testclient"):
            await self.app(scope, receive, send)
            return
        if _header(scope, b"x-api-key") != self.api_key.encode():
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_security_middleware(app, settings) -> None:
    if settings.app_api_key:
        app.add_middleware(
            ApiKeyMiddleware, api_key=settings.app_api_key, trust_loopback=settings.trust_loopback
        )
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.rate_per_minute,
        burst=settings.rate_burst,
    )
    app.add_middleware(BodyLimitMiddleware, max_bytes=settings.max_body_bytes)
    # Starlette runs the most recently added middleware first, so headers wrap early 401/413/429
    # responses and errors from every inner middleware.
    app.add_middleware(SecurityHeadersMiddleware)

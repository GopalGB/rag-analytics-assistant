"""HTTP hardening: body-size limit, per-IP rate limiting, optional API key, security headers, a Host
allowlist (DNS-rebinding defence) and a same-origin check on state-changing requests (CSRF defence)."""

from __future__ import annotations

import hmac
import re
import time
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

PUBLIC_PREFIXES = ("/health", "/ready", "/ui", "/static")

# Strict CSP for the app: scripts only from this origin (no inline JS), no framing, no plugins, no
# third-party connections. Inline styles are allowed (the UI sets style attributes on elements).
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'"
)
PUBLIC_EXACT = {"/", "/favicon.ico"}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if not request.url.path.startswith("/files/"):  # original PDFs open in the browser's viewer
            response.headers.setdefault("Content-Security-Policy", CSP)
        return response


class BodyLimitMiddleware:
    """Rejects oversized bodies by counting the bytes actually received, so a chunked request without
    Content-Length can't slip past. `overrides` maps exact paths (e.g. /upload) to a larger limit.
    Pure ASGI: the body is read up to the limit, then replayed to the app; after that the app gets the
    real `receive`, so streaming responses still notice when the client disconnects."""

    def __init__(self, app, max_bytes: int, overrides: dict[str, int] | None = None):
        self.app = app
        self.max_bytes = max_bytes
        self.overrides = overrides or {}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.overrides.get(scope.get("path", ""), self.max_bytes)
        too_large = JSONResponse({"error": "request body too large"}, status_code=413)
        cl = dict(scope.get("headers") or []).get(b"content-length", b"")
        if cl.isdigit() and int(cl) > limit:
            return await too_large(scope, receive, send)
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > limit:
                return await too_large(scope, receive, send)
            chunks.append(body)
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP token bucket; only meters mutating (POST) requests. State is bounded: idle clients are
    forgotten after `ttl` seconds and at most `max_clients` are tracked (oldest evicted first)."""

    def __init__(self, app, per_minute: int, burst: int, max_clients: int = 4096, ttl: float = 300.0):
        super().__init__(app)
        self.rate = per_minute / 60.0
        self.burst = burst
        self.max_clients = max_clients
        self.ttl = ttl
        self._state: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last seen)

    def _evict(self, now: float) -> None:
        for ip in [ip for ip, (_, seen) in self._state.items() if now - seen > self.ttl]:
            del self._state[ip]
        while len(self._state) >= self.max_clients:
            del self._state[min(self._state, key=lambda k: self._state[k][1])]

    async def dispatch(self, request: Request, call_next):
        if request.method != "POST":
            return await call_next(request)
        ip = _client_ip(request)
        now = time.monotonic()
        if ip not in self._state and len(self._state) >= self.max_clients:
            self._evict(now)
        tokens, last = self._state.get(ip, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1.0:
            self._state[ip] = (tokens, now)
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        self._state[ip] = (tokens - 1.0, now)
        return await call_next(request)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """If an API key is configured, require it on non-public paths (loopback is exempt)."""

    def __init__(self, app, api_key: str, trust_loopback: bool = True):
        super().__init__(app)
        self.api_key = api_key
        self.trust_loopback = trust_loopback

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in PUBLIC_EXACT or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        # Loopback exemption is opt-in: disable it behind a reverse proxy, where every request
        # would otherwise appear to originate from 127.0.0.1 and bypass the key.
        if self.trust_loopback and _client_ip(request) in ("127.0.0.1", "::1", "testclient"):
            return await call_next(request)
        if not hmac.compare_digest(request.headers.get("x-api-key", "").encode(), self.api_key.encode()):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Requests that change state; refused when PUBLIC_DEMO=true (the hosted demo is read-only).
DEMO_BLOCKED = [
    ("POST", re.compile(r"^/(refresh|upload|qbo/sync|qbo/disconnect|approvals)$")),
    ("POST", re.compile(r"^/(invoices|approvals)/[^/]+/(review|decision)$")),
    ("GET", re.compile(r"^/qbo/(connect|callback)$")),
]


class PublicDemoMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(request.method == m and rx.match(path) for m, rx in DEMO_BLOCKED):
            return JSONResponse({"error": "This is a read-only public demo: that action is disabled."}, status_code=403)
        return await call_next(request)


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Reject cross-site requests that change state. The API key's loopback exemption means a page on
    any website could otherwise make the user's browser POST to 127.0.0.1 (upload, sync, approve...).
    Browsers send Sec-Fetch-Site and/or Origin on such requests; clients that send neither (curl,
    scripts) are not a browser acting for another site and are left to the API key."""

    def __init__(self, app, allowed_origins: list[str] | None = None):
        super().__init__(app)
        # Extra origins a trusted proxy serves the UI from (e.g. https://example.com in front of a
        # hosted demo), compared exactly as scheme://host[:port].
        self.allowed_origins = {o.rstrip("/").lower() for o in (allowed_origins or [])}

    async def dispatch(self, request: Request, call_next):
        if request.method not in SAFE_METHODS and not same_origin(request, self.allowed_origins):
            return JSONResponse({"error": "cross-site request blocked"}, status_code=403)
        return await call_next(request)


def same_origin(request: Request, allowed_origins: set[str] | frozenset[str] = frozenset()) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True
    if origin.rstrip("/").lower() in allowed_origins:
        return True
    parts = urlsplit(origin)
    host = request.headers.get("host", "")
    return parts.scheme in ("http", "https") and parts.netloc.lower() == host.lower()


def install_security_middleware(app, settings) -> None:
    # Order matters: headers outermost, then auth, rate, body (added last = runs first).
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.app_api_key:
        app.add_middleware(
            ApiKeyMiddleware, api_key=settings.app_api_key, trust_loopback=settings.trust_loopback
        )
    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.rate_per_minute,
        burst=settings.rate_burst,
    )
    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=settings.max_body_bytes,
        overrides={"/upload": settings.max_upload_bytes + 64 * 1024},
    )
    app.add_middleware(SameOriginMiddleware, allowed_origins=settings.allowed_origin_list())
    if settings.public_demo:
        app.add_middleware(PublicDemoMiddleware)
    hosts = settings.allowed_host_list()
    if hosts and "*" not in hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

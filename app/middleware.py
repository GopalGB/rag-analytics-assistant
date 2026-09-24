"""HTTP hardening: body-size limit, per-IP rate limiting, optional API key, security headers."""

from __future__ import annotations

import time
from collections import defaultdict

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

PUBLIC_PREFIXES = ("/health", "/ui", "/static")
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
        return response


class BodyLimitMiddleware(BaseHTTPMiddleware):
    """Rejects oversized bodies. `overrides` maps exact paths (e.g. /upload) to a larger limit."""

    def __init__(self, app, max_bytes: int, overrides: dict[str, int] | None = None):
        super().__init__(app)
        self.max_bytes = max_bytes
        self.overrides = overrides or {}

    async def dispatch(self, request: Request, call_next):
        limit = self.overrides.get(request.url.path, self.max_bytes)
        cl = request.headers.get("content-length")
        if cl is not None and cl.isdigit() and int(cl) > limit:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple per-IP token bucket; only meters mutating (POST) requests."""

    def __init__(self, app, per_minute: int, burst: int):
        super().__init__(app)
        self.rate = per_minute / 60.0
        self.burst = burst
        self._tokens: dict[str, float] = defaultdict(lambda: float(burst))
        self._last: dict[str, float] = defaultdict(time.monotonic)

    async def dispatch(self, request: Request, call_next):
        if request.method != "POST":
            return await call_next(request)
        ip = _client_ip(request)
        now = time.monotonic()
        self._tokens[ip] = min(self.burst, self._tokens[ip] + (now - self._last[ip]) * self.rate)
        self._last[ip] = now
        if self._tokens[ip] < 1.0:
            return JSONResponse({"error": "rate limit exceeded"}, status_code=429)
        self._tokens[ip] -= 1.0
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
        if request.headers.get("x-api-key") != self.api_key:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


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

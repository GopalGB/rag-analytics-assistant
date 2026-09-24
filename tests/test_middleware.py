"""ApiKeyMiddleware: enforcement, public-path bypass, and the loopback exemption toggle."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import (
    ApiKeyMiddleware,
    BodyLimitMiddleware,
    SecurityHeadersMiddleware,
    install_security_middleware,
)


def _client(trust_loopback: bool) -> TestClient:
    app = FastAPI()

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/private")
    def private() -> dict:
        return {"ok": True}

    app.add_middleware(ApiKeyMiddleware, api_key="topsecret", trust_loopback=trust_loopback)
    return TestClient(app)


def test_blocks_without_key_when_loopback_untrusted():
    assert _client(trust_loopback=False).get("/private").status_code == 401


def test_allows_with_correct_key():
    r = _client(trust_loopback=False).get("/private", headers={"x-api-key": "topsecret"})
    assert r.status_code == 200


def test_rejects_wrong_key():
    r = _client(trust_loopback=False).get("/private", headers={"x-api-key": "nope"})
    assert r.status_code == 401


def test_public_path_bypasses_auth():
    assert _client(trust_loopback=False).get("/health").status_code == 200


def test_loopback_exempt_when_trusted():
    # TestClient presents as a loopback caller; trusted mode lets it through without a key.
    assert _client(trust_loopback=True).get("/private").status_code == 200


def test_body_limit_checks_actual_body_and_headers_survive_413():
    app = FastAPI()

    @app.post("/upload")
    async def upload(payload: dict) -> dict:
        return payload

    app.add_middleware(BodyLimitMiddleware, max_bytes=4)
    app.add_middleware(SecurityHeadersMiddleware)
    response = TestClient(app).post("/upload", content=b'{"too":"large"}')
    assert response.status_code == 413
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_body_limit_replays_a_valid_post_body():
    app = FastAPI()

    @app.post("/upload")
    async def upload(payload: dict) -> dict:
        return payload

    app.add_middleware(BodyLimitMiddleware, max_bytes=64)
    response = TestClient(app).post("/upload", json={"ok": True})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_body_limit_rejects_a_streamed_body_after_its_actual_bytes():
    sent: list[dict] = []
    messages = iter(
        [
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"de", "more_body": False},
        ]
    )

    async def receive() -> dict:
        return next(messages)

    async def downstream(scope, receive, send) -> None:
        raise AssertionError("oversized request reached downstream")

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}
    asyncio.run(BodyLimitMiddleware(downstream, max_bytes=4)(scope, receive, send))
    assert sent[0]["status"] == 413


def test_body_limit_forwards_a_client_disconnect():
    received: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.disconnect"}

    async def downstream(scope, replay, send) -> None:
        received.append(await replay())

    async def send(message: dict) -> None:
        raise AssertionError(f"disconnect should not produce a response: {message}")

    scope = {"type": "http", "method": "POST", "path": "/upload", "headers": []}
    asyncio.run(BodyLimitMiddleware(downstream, max_bytes=4)(scope, receive, send))
    assert received == [{"type": "http.disconnect"}]


def test_installed_headers_cover_early_responses_and_errors():
    app = FastAPI()

    @app.post("/private")
    async def private(payload: dict) -> dict:
        return payload

    @app.get("/explode")
    async def explode() -> None:
        raise RuntimeError("expected test error")

    install_security_middleware(
        app,
        SimpleNamespace(
            app_api_key="topsecret",
            trust_loopback=False,
            rate_per_minute=60,
            rate_burst=2,
            max_body_bytes=4,
        ),
    )
    client = TestClient(app, raise_server_exceptions=False)
    responses = [
        client.post("/private", content=b"{}"),
        client.post("/private", headers={"x-api-key": "topsecret"}, content=b"12345"),
        client.post(
            "/private",
            headers={"x-api-key": "topsecret", "content-type": "application/json"},
            content=b"{}",
        ),
        client.post(
            "/private",
            headers={"x-api-key": "topsecret", "content-type": "application/json"},
            content=b"{}",
        ),
        client.get("/explode", headers={"x-api-key": "topsecret"}),
    ]
    assert [response.status_code for response in responses] == [401, 413, 200, 429, 500]
    assert all(response.headers["X-Content-Type-Options"] == "nosniff" for response in responses)


def test_security_headers_reraise_after_response_started():
    # Once headers are sent, a second 500 start is illegal; the original error must propagate.
    import pytest
    from starlette.responses import StreamingResponse

    app = FastAPI()

    def broken_stream():
        yield b"partial"
        raise ValueError("stream failed")

    @app.get("/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(broken_stream())

    app.add_middleware(SecurityHeadersMiddleware)
    with pytest.raises(ValueError, match="stream failed"):
        TestClient(app).get("/stream")

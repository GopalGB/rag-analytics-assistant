"""ApiKeyMiddleware: enforcement, public-path bypass, and the loopback exemption toggle."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import ApiKeyMiddleware


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

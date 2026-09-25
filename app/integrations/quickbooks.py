"""QuickBooks Online — READ-ONLY integration.

Two interchangeable clients:

- `MockQuickBooks`  — serves a bundled, fictional sandbox-company fixture in the exact QuickBooks API
                      response shape. Fully offline; used for demos and tests. (QBO_MODE=mock)
- `QuickBooksOnline`— the real QuickBooks Online Accounting API against an Intuit *sandbox* company,
                      via OAuth 2.0. (QBO_MODE=sandbox)

Read-only is enforced HERE, in code, not just by policy: Intuit's accounting scope
(`com.intuit.quickbooks.accounting`) grants read AND write, so this client only ever issues HTTP GET
requests to the `/query` endpoint, and every query must be a single `SELECT * FROM <Entity>` over an
allowlisted entity. There is no code path that can create, update, or delete a record.

Production companies are refused unless QBO_ALLOW_PRODUCTION=true (not needed for this stage).
Tokens are stored locally (0600 file, or the macOS Keychain) and can be revoked from the UI.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

READ_ENTITIES = {"CompanyInfo", "Vendor", "Customer", "Bill", "Invoice", "Account", "Purchase", "BillPayment", "Payment"}
_QUERY_RE = re.compile(
    r"^\s*select\s+\*\s+from\s+([A-Za-z]+)(\s+where\s+[\w\s'=<>.\-:]+)?(\s+orderby\s+[\w\s,]+)?"
    r"(\s+startposition\s+\d+)?(\s+maxresults\s+\d+)?\s*$",
    re.I,
)

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
API_BASE = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}
SCOPE = "com.intuit.quickbooks.accounting"
MINOR_VERSION = "75"


class ReadOnlyViolation(ValueError):
    """Raised for anything other than a single SELECT over an allowlisted entity."""


def validate_query(query: str) -> str:
    """Return the entity name if `query` is an allowed read-only query, else raise."""
    if ";" in query:
        raise ReadOnlyViolation("multiple statements are not allowed")
    m = _QUERY_RE.match(query)
    if not m:
        raise ReadOnlyViolation("only 'SELECT * FROM <Entity>' queries are allowed")
    entity = next((e for e in READ_ENTITIES if e.lower() == m.group(1).lower()), None)
    if entity is None:
        raise ReadOnlyViolation(f"entity not allowed: {m.group(1)}")
    return entity


class QuickBooksClient(Protocol):
    mode: str

    @property
    def connected(self) -> bool: ...

    def query(self, query: str) -> list[dict[str, Any]]: ...


# --------------------------------------------------------------------------- mock (offline)
class MockQuickBooks:
    """Offline sandbox fixture. Same read-only query surface as the live client."""

    mode = "mock"

    def __init__(self, fixture_path: str | Path):
        self.fixture_path = Path(fixture_path)
        self._data: dict[str, list[dict[str, Any]]] = {}
        if self.fixture_path.exists():
            self._data = json.loads(self.fixture_path.read_text(encoding="utf-8"))

    @property
    def connected(self) -> bool:
        return bool(self._data)

    @property
    def realm_id(self) -> str:
        return "mock-sandbox"

    def query(self, query: str) -> list[dict[str, Any]]:
        entity = validate_query(query)
        return [dict(row) for row in self._data.get(entity, [])]


# --------------------------------------------------------------------------- token storage
class TokenStore:
    """Stores OAuth tokens locally. `file` = JSON with 0600 permissions; `keyring` = OS keychain."""

    SERVICE = "private-ai-assistant-qbo"

    def __init__(self, path: str | Path, backend: str = "file"):
        self.path = Path(path)
        self.backend = backend

    def load(self) -> dict[str, Any] | None:
        raw: str | None = None
        if self.backend == "keyring":
            import keyring  # optional dependency: pip install keyring

            raw = keyring.get_password(self.SERVICE, "tokens")
        elif self.path.exists():
            raw = self.path.read_text(encoding="utf-8")
        return json.loads(raw) if raw else None

    def save(self, tokens: dict[str, Any]) -> None:
        raw = json.dumps(tokens)
        if self.backend == "keyring":
            import keyring

            keyring.set_password(self.SERVICE, "tokens", raw)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(raw)
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        if self.backend == "keyring":
            import keyring

            try:
                keyring.delete_password(self.SERVICE, "tokens")
            except Exception:
                pass
        elif self.path.exists():
            self.path.unlink()


# --------------------------------------------------------------------------- live (sandbox) client
class QuickBooksOnline:
    mode = "sandbox"

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str,
        token_store: TokenStore,
        environment: str = "sandbox",
        allow_production: bool = False,
        http: Any = None,
    ):
        if environment == "production" and not allow_production:
            raise ValueError("Production QuickBooks access is disabled for the prototype (QBO_ALLOW_PRODUCTION).")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.tokens = token_store
        self.environment = environment
        self.mode = environment
        self._states: dict[str, float] = {}
        if http is None:
            import requests

            http = requests
        self.http = http

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def connected(self) -> bool:
        t = self.tokens.load()
        return bool(t and t.get("refresh_token") and t.get("realm_id"))

    @property
    def realm_id(self) -> str | None:
        t = self.tokens.load()
        return t.get("realm_id") if t else None

    # ---- OAuth 2.0 -----------------------------------------------------
    def authorize_url(self) -> str:
        if not self.configured:
            raise ValueError("Set QBO_CLIENT_ID and QBO_CLIENT_SECRET (from your Intuit developer sandbox app).")
        state = secrets.token_urlsafe(24)
        self._states[state] = time.time()
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "scope": SCOPE,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def handle_callback(self, code: str, state: str, realm_id: str) -> None:
        issued = self._states.pop(state, None)
        if issued is None or time.time() - issued > 600:
            raise ValueError("invalid or expired OAuth state")
        if not re.fullmatch(r"\d{1,32}", realm_id or ""):
            raise ValueError("invalid realmId")
        data = self._token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri})
        data["realm_id"] = realm_id
        self.tokens.save(data)

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        resp = self.http.post(
            TOKEN_URL,
            data=form,
            auth=(self.client_id, self.client_secret),
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        now = time.time()
        return {
            "access_token": body["access_token"],
            "refresh_token": body["refresh_token"],
            "access_expires_at": now + int(body.get("expires_in", 3600)) - 60,
            "refresh_expires_at": now + int(body.get("x_refresh_token_expires_in", 8640000)),
        }

    def _access_token(self) -> tuple[str, str]:
        t = self.tokens.load()
        if not t:
            raise PermissionError("QuickBooks is not connected")
        if time.time() >= t.get("access_expires_at", 0):
            fresh = self._token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
            fresh["realm_id"] = t["realm_id"]
            self.tokens.save(fresh)
            t = fresh
        return t["access_token"], t["realm_id"]

    def revoke(self) -> None:
        """Revoke the refresh token at Intuit (best effort) and delete it locally."""
        t = self.tokens.load()
        try:
            if t and self.configured:
                self.http.post(
                    REVOKE_URL,
                    json={"token": t["refresh_token"]},
                    auth=(self.client_id, self.client_secret),
                    headers={"Accept": "application/json"},
                    timeout=30,
                )
        finally:
            self.tokens.clear()

    # ---- read-only query -----------------------------------------------
    def query(self, query: str) -> list[dict[str, Any]]:
        entity = validate_query(query)
        token, realm = self._access_token()
        url = f"{API_BASE[self.environment]}/v3/company/{realm}/query"
        rows: list[dict[str, Any]] = []
        start, page = 1, 500
        while True:
            q = f"{query.strip()} STARTPOSITION {start} MAXRESULTS {page}"
            resp = self.http.get(  # GET only — this client never issues a write
                url,
                params={"query": q, "minorversion": MINOR_VERSION},
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json().get("QueryResponse", {}).get(entity, [])
            rows.extend(batch)
            if len(batch) < page:
                return rows
            start += page

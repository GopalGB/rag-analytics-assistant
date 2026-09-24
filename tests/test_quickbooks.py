"""QuickBooks: read-only enforcement, OAuth token handling, sync + reconciliation on the sandbox fixture."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.accounting import qbo_sync, reconcile, reports
from app.data.store import DataStore
from app.integrations.quickbooks import (
    MockQuickBooks,
    QuickBooksOnline,
    ReadOnlyViolation,
    TokenStore,
    validate_query,
)
from app.invoices.registry import InvoiceRecord

FIXTURE = Path(__file__).resolve().parent.parent / "data" / "qbo_sandbox" / "sandbox_company.json"


@pytest.mark.parametrize(
    "query",
    [
        "delete from Bill where Id = '1'",
        "select * from Bill; select * from Vendor",
        "select * from Employee",  # not on the allowlist
        "update Bill set TotalAmt = 0",
        "select Id from Bill",
    ],
)
def test_only_allowlisted_selects(query):
    with pytest.raises(ReadOnlyViolation):
        validate_query(query)


def test_allowed_query():
    assert validate_query("SELECT * FROM bill WHERE Balance > '0'") == "Bill"


def test_mock_client_serves_fixture():
    qb = MockQuickBooks(FIXTURE)
    assert qb.connected
    assert len(qb.query("select * from Bill")) == 8


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeHTTP:
    """Records calls; serves a token endpoint and a paginated query endpoint."""

    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return _Resp({"access_token": "AT", "refresh_token": "RT2", "expires_in": 3600, "x_refresh_token_expires_in": 86400})

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))
        start = int(params["query"].split("STARTPOSITION ")[1].split()[0])
        batch = [{"Id": str(i)} for i in range(start, min(start + 500, 1 + 700))]
        return _Resp({"QueryResponse": {"Vendor": batch}})

    def put(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("write attempted")

    delete = patch = put


def _client(tmp_path, http=None):
    store = TokenStore(tmp_path / "secrets" / "tokens.json")
    return QuickBooksOnline("cid", "secret", "http://localhost:8000/qbo/callback", store, http=http or _FakeHTTP())


def test_oauth_flow_state_and_token_file_permissions(tmp_path):
    qb = _client(tmp_path)
    url = qb.authorize_url()
    params = parse_qs(urlparse(url).query)
    assert params["scope"] == ["com.intuit.quickbooks.accounting"]
    with pytest.raises(ValueError):
        qb.handle_callback("code", "wrong-state", "123")
    qb.handle_callback("code", params["state"][0], "4620816365")
    assert qb.connected and qb.realm_id == "4620816365"
    mode = stat.S_IMODE(os.stat(tmp_path / "secrets" / "tokens.json").st_mode)
    assert mode == 0o600
    with pytest.raises(ValueError):  # state is single-use
        qb.handle_callback("code", params["state"][0], "4620816365")


def test_live_client_is_get_only_and_paginates(tmp_path):
    http = _FakeHTTP()
    qb = _client(tmp_path, http)
    qb.tokens.save({"access_token": "AT", "refresh_token": "RT", "access_expires_at": 0, "realm_id": "1"})
    rows = qb.query("select * from Vendor")
    assert len(rows) == 700
    gets = [c for c in http.calls if c[0] == "GET"]
    assert len(gets) == 2 and all("/v3/company/1/query" in c[1] for c in gets)
    assert all(c[1].startswith("https://sandbox-quickbooks.api.intuit.com") for c in gets)
    # the expired access token was refreshed (a POST to the token endpoint, never to the API)
    posts = [c for c in http.calls if c[0] == "POST"]
    assert posts and all("oauth.platform.intuit.com" in c[1] for c in posts)
    with pytest.raises(ReadOnlyViolation):
        qb.query("delete from Vendor")


def test_revoke_clears_tokens(tmp_path):
    qb = _client(tmp_path)
    qb.tokens.save({"access_token": "AT", "refresh_token": "RT", "access_expires_at": 9e12, "realm_id": "1"})
    qb.revoke()
    assert not qb.connected and not (tmp_path / "secrets" / "tokens.json").exists()


def test_production_refused_by_default(tmp_path):
    with pytest.raises(ValueError):
        QuickBooksOnline("a", "b", "c", TokenStore(tmp_path / "t.json"), environment="production", http=_FakeHTTP())


def _rec(file, supplier, number, date, total, **kw):
    return InvoiceRecord(
        id=file, file=file, sha256=file, values={"supplier": supplier, "invoice_number": number, "invoice_date": date,
                                                 "total": total}, field_confidence={}, field_notes={}, field_evidence={},
        confidence=0.9, issues=[], method="rules", ocr=False, **kw,
    )


def test_sync_and_reconcile(tmp_path):
    store = DataStore(str(tmp_path / "q.duckdb"))
    try:
        counts = qbo_sync.sync(MockQuickBooks(FIXTURE), store)
        assert counts["qbo_bills"] == 8 and counts["qbo_invoices"] == 5
        records = [
            _rec("a", "Summit Ridge Electrical Supply", "INV-10421", "2026-05-04", 4871.25),
            _rec("b", "Coastal Plumbing & Heating", "5530", "2026-05-18", 2310.00),
            _rec("c", "Harbor Waste Services", None, "2026-05-28", 415.00),
            _rec("d", "Metro Office Solutions Inc", "MOS-7781", "2026-05-15", 689.42),
            _rec("e", "Summit Ridge Electrical Supply LLC", "INV-10421", "2026-05-04", 4871.25, duplicate_of="a"),
            _rec("f", "Somebody", "X-1", "2026-05-04", 1.0, status="rejected"),
        ]
        rows = reconcile.reconcile(records, store)
        status = {r["file"]: r["status"] for r in rows if r["file"]}
        assert status == {"a": "matched", "b": "amount_mismatch", "c": "possible_match", "d": "not_in_quickbooks",
                          "e": "duplicate", "f": "rejected"}
        mismatch = next(r for r in rows if r["file"] == "b")
        assert mismatch["difference"] == 180.0
        orphans = {r["invoice_number"] for r in rows if r["status"] == "no_document"}
        assert "AFC-3302" in orphans
        reconcile.load_reconciliation(store, rows)
        summary = reports.build_summary(store)
        assert summary["sections"]["payables"]["open_bills"] == 5  # Harbor Waste bill is marked paid
        assert "Needs attention" in reports.to_markdown(summary)
    finally:
        store.close()

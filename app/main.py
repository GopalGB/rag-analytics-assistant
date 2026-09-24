"""FastAPI entrypoint. Run with: uvicorn app.main:app --host 127.0.0.1 --port 8000"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from app.config import Settings
from app.data import watcher
from app.integrations.quickbooks import QuickBooksOnline
from app.middleware import install_security_middleware
from app.workspace import LLMNotConfiguredError, Workspace  # noqa: F401  (re-exported for callers)

_UI_FILE = Path(__file__).parent / "ui" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    ws = Workspace(settings)
    ws.startup()
    app.state.settings = settings
    app.state.ws = ws
    app.state.engine = ws.engine
    # Auto-ingest: watch the data dir and process new or changed files on the fly.
    app.state.watch_stop = asyncio.Event()
    app.state.watch_task = None
    if settings.auto_reindex:
        app.state.watch_task = asyncio.create_task(
            watcher.run_watcher(ws.reindex, settings.data_dir, settings.reindex_interval_seconds, app.state.watch_stop)
        )
    try:
        yield
    finally:
        if app.state.watch_task is not None:
            app.state.watch_stop.set()
            app.state.watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.state.watch_task
        ws.store.close()


app = FastAPI(title="Private AI Assistant", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
install_security_middleware(app, Settings())


def _ws(request: Request) -> Workspace:
    return request.app.state.ws


def _actor(request: Request) -> str:
    """Display name of the person using the UI (for the activity log). Not an authentication factor."""
    name = re.sub(r"[^\w .@-]", "", request.headers.get("x-user", "")).strip()[:60]
    return name or "local-user"


# --------------------------------------------------------------------------- UI + status
@app.get("/")
def index() -> FileResponse:
    return FileResponse(_UI_FILE, media_type="text/html")


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/health")
def health(request: Request) -> dict:
    ws = _ws(request)
    return {
        "status": "ok",
        "app_name": ws.settings.app_name,
        **ws.engine.status(),
        "llm_note": ws.llm_note,
        "ocr": ws.ocr.name,
        "invoices": len(ws.invoices.records),
        "qbo": ws.qbo_status(),
        "pending_approvals": len(ws.approvals.list("pending")),
        "last_reindex": ws.last_reindex,
    }


@app.get("/examples")
def examples() -> dict:
    return {
        "examples": [
            "What are the payment terms in the Summit Ridge supply agreement?",
            "When does the office lease expire and what notice is needed to renew?",
            "What tasks are outstanding on the Riverside renovation project?",
            "Which invoices need director approval under our policy?",
            "Which supplier invoices don't match QuickBooks?",
            "Which customers have overdue balances?",
            "What is the monthly fee in the Coastal Plumbing maintenance agreement?",
            "Who is our auditor?",
        ]
    }


# --------------------------------------------------------------------------- ask
class ChatIn(BaseModel):
    question: str = Field(default="", max_length=8000)
    session_id: str = Field(default="default", max_length=128)


@app.post("/chat")
def chat(body: ChatIn, request: Request) -> dict:
    return _ws(request).engine.answer(body.session_id, body.question.strip(), actor=_actor(request))


@app.post("/refresh")
def refresh(request: Request) -> dict:
    # Manual trigger for the same reindex the auto-watcher runs.
    return {"status": "refreshed", **_ws(request).reindex(actor=_actor(request))}


# --------------------------------------------------------------------------- documents
@app.get("/documents")
def documents(request: Request) -> dict:
    return {"documents": _ws(request).documents_view()}


@app.get("/files/{path:path}")
def get_file(path: str, request: Request) -> FileResponse:
    """Serve an original source document (for 'open source' links). Confined to the data dir."""
    root = Path(_ws(request).settings.data_dir).resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target, content_disposition_type="inline")


@app.post("/upload")
async def upload(request: Request, file: Annotated[UploadFile, File()], kind: Annotated[str, Form()] = "auto") -> dict:
    ws = _ws(request)
    if kind not in ("auto", "invoice", "document", "table"):
        raise HTTPException(status_code=400, detail="kind must be auto, invoice, document or table")
    data = await file.read(ws.settings.max_upload_bytes + 1)
    try:
        return await asyncio.to_thread(ws.save_upload, file.filename or "", data, kind, _actor(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- invoices
class ReviewIn(BaseModel):
    status: str
    reviewer: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=500)
    corrections: dict[str, Any] = Field(default_factory=dict)


@app.get("/invoices")
def invoices(request: Request) -> dict:
    return {"invoices": [r.to_dict() for r in _ws(request).invoices.records]}


@app.post("/invoices/{invoice_id}/review")
def review_invoice(invoice_id: str, body: ReviewIn, request: Request) -> dict:
    ws = _ws(request)
    try:
        rec = ws.invoices.review(invoice_id, body.status, body.reviewer, body.corrections, body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="invoice not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ws.store.load_dataframe("invoices", ws.invoices.dataframe())
    ws._reconcile()
    ws.audit.record(
        "invoice.reviewed", actor=body.reviewer or _actor(request), file=rec.file, status=rec.status,
        corrected_fields=sorted(body.corrections), note=body.note[:200],
    )
    return rec.to_dict()


@app.get("/invoices/export.csv")
def export_invoices(request: Request) -> Response:
    buf = io.StringIO()
    _ws(request).invoices.dataframe().to_csv(buf, index=False)
    return Response(
        buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="invoices.csv"'}
    )


# --------------------------------------------------------------------------- QuickBooks (read-only)
@app.get("/qbo/status")
def qbo_status(request: Request) -> dict:
    return _ws(request).qbo_status()


@app.post("/qbo/sync")
def qbo_sync(request: Request) -> dict:
    try:
        return _ws(request).sync_qbo(actor=_actor(request))
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/qbo/connect")
def qbo_connect(request: Request) -> Response:
    ws = _ws(request)
    if not isinstance(ws.qbo, QuickBooksOnline):
        raise HTTPException(status_code=400, detail="Set QBO_MODE=sandbox to connect a live QuickBooks sandbox company.")
    try:
        url = ws.qbo.authorize_url()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ws.audit.record("qbo.connect_started", actor=_actor(request), environment=ws.qbo.environment)
    return RedirectResponse(url)


@app.get("/qbo/callback")
def qbo_callback(request: Request, code: str = "", state: str = "", realmId: str = "") -> Response:  # noqa: N803
    ws = _ws(request)
    if not isinstance(ws.qbo, QuickBooksOnline):
        raise HTTPException(status_code=400, detail="QuickBooks sandbox mode is not enabled.")
    try:
        ws.qbo.handle_callback(code, state, realmId)
    except Exception as exc:
        ws.audit.record("qbo.connect_failed", actor=_actor(request), error=type(exc).__name__)
        raise HTTPException(status_code=400, detail="QuickBooks connection failed.") from exc
    ws.audit.record("qbo.connected", actor=_actor(request), realm_id=realmId, environment=ws.qbo.environment)
    return RedirectResponse("/#quickbooks")


@app.post("/qbo/disconnect")
def qbo_disconnect(request: Request) -> dict:
    ws = _ws(request)
    if isinstance(ws.qbo, QuickBooksOnline):
        ws.qbo.revoke()
    for table in [t for t in ws.store.tables() if t.startswith("qbo_") or t == "invoice_reconciliation"]:
        ws.store.drop_table(table)
    ws.last_qbo_sync = None
    ws.audit.record("qbo.disconnected", actor=_actor(request), tokens_revoked=isinstance(ws.qbo, QuickBooksOnline))
    return {"status": "disconnected", "note": "Access revoked and the local QuickBooks copy was deleted."}


@app.get("/qbo/data")
def qbo_data(request: Request) -> dict:
    ws = _ws(request)
    out: dict[str, Any] = {}
    queries = {
        "bills": "SELECT id, doc_number, vendor_name, txn_date, due_date, total, balance, status, days_overdue "
        "FROM qbo_bills ORDER BY txn_date",
        "invoices": "SELECT id, doc_number, customer_name, txn_date, due_date, total, balance, status, days_overdue "
        "FROM qbo_invoices ORDER BY txn_date",
        "accounts": "SELECT name, account_type, current_balance FROM qbo_accounts ORDER BY account_type, name",
    }
    tables = set(ws.store.tables())
    for key, sql in queries.items():
        if f"qbo_{key}" in tables:
            cols, rows = ws.store.run_select(sql, max_rows=500)
            out[key] = [dict(zip(cols, r, strict=False)) for r in rows]
    return out


@app.get("/reconciliation")
def reconciliation(request: Request) -> dict:
    ws = _ws(request)
    if "invoice_reconciliation" not in ws.store.tables():
        return {"rows": []}
    cols, rows = ws.store.run_select(
        "SELECT * FROM invoice_reconciliation ORDER BY CASE severity WHEN 'issue' THEN 0 WHEN 'warning' THEN 1 "
        "ELSE 2 END, supplier",
        max_rows=2000,
    )
    return {"rows": [dict(zip(cols, r, strict=False)) for r in rows]}


@app.get("/reports/summary")
def summary_report(request: Request) -> dict:
    return _ws(request).summary_report()


@app.get("/reports/summary.md")
def summary_report_md(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        _ws(request).summary_report()["markdown"],
        media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="accounts-summary-draft.md"'},
    )


# --------------------------------------------------------------------------- approvals
class ProposeIn(BaseModel):
    action_type: str = Field(max_length=40)
    title: str = Field(max_length=200)
    details: str = Field(default="", max_length=4000)
    dedupe_key: str | None = Field(default=None, max_length=200)


class DecisionIn(BaseModel):
    decision: str
    reviewer: str = Field(default="", max_length=80)
    note: str = Field(default="", max_length=500)


@app.get("/approvals")
def approvals(request: Request, status: str | None = None) -> dict:
    return {"items": _ws(request).approvals.list(status)}


@app.post("/approvals")
def propose(body: ProposeIn, request: Request) -> dict:
    ws = _ws(request)
    item = ws.approvals.propose(body.action_type, body.title, body.details, _actor(request), body.dedupe_key)
    ws.audit.record("action.proposed", actor=_actor(request), id=item["id"], action=item["action_type"], title=item["title"])
    return item


@app.post("/approvals/{item_id}/decision")
def decide(item_id: str, body: DecisionIn, request: Request) -> dict:
    ws = _ws(request)
    try:
        item = ws.approvals.decide(item_id, body.decision, body.reviewer, body.note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    ws.audit.record(
        f"action.{item['status']}", actor=item["decided_by"], id=item_id, action=item["action_type"],
        title=item["title"], executed=False,
    )
    return item


# --------------------------------------------------------------------------- AI routing
@app.get("/router")
def router_view(request: Request) -> dict:
    """Configured models per tier, fallback order, health/circuit state, usage and cost, privacy policy."""
    return _ws(request).router_view()


# --------------------------------------------------------------------------- audit + privacy
@app.get("/audit")
def audit(request: Request, limit: int = 200) -> dict:
    ws = _ws(request)
    return {"entries": list(reversed(ws.audit.tail(max(1, min(limit, 2000))))), "integrity": ws.audit.verify()}


@app.get("/privacy")
def privacy(request: Request) -> dict:
    return _ws(request).privacy_report()


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    # Never leak internals to the client.
    return JSONResponse({"error": "internal error"}, status_code=500)

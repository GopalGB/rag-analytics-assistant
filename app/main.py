"""FastAPI entrypoint. Run with: uvicorn app.main:app --host 127.0.0.1 --port 8000"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import unquote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, Field

from app import __version__
from app.accounting.reports import REPORTS, markdown_to_html
from app.agent.streaming import stream_answer
from app.config import Settings
from app.data import watcher
from app.integrations.quickbooks import QuickBooksOnline
from app.invoices.extract import extract_invoice
from app.middleware import install_security_middleware
from app.observability import METRICS, RequestContextMiddleware, event, log, request_id_var, setup_logging
from app.workspace import LLMNotConfiguredError, Workspace  # noqa: F401  (re-exported for callers)

_UI_FILE = Path(__file__).parent / "ui" / "index.html"
_UI_ASSETS = {"charts.js": "text/javascript", "app.js": "text/javascript"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    setup_logging(settings.log_level, settings.log_format)
    t0 = time.perf_counter()
    ws = Workspace(settings)
    ws.startup()
    event("startup_complete", version=__version__, ms=round((time.perf_counter() - t0) * 1000),
          documents=len(ws.engine.documents), models=[m.name for m in ws.router.models()],
          embedding=ws.engine.retriever.embeddings.name)
    for w in ws.config_warnings:
        event("config_warning", level=logging.WARNING, warning=w)
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


app = FastAPI(title="Private AI Assistant", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
install_security_middleware(app, Settings())
app.add_middleware(RequestContextMiddleware)  # outermost: request ID + access log + metrics for everything


def _ws(request: Request) -> Workspace:
    return request.app.state.ws


def _actor(request: Request) -> str:
    """Display name of the person using the UI (for the activity log). Not an authentication factor."""
    name = re.sub(r"[^\w .@-]", "", unquote(request.headers.get("x-user", ""))).strip()[:60]
    return name or "local-user"


# --------------------------------------------------------------------------- UI + status
@app.get("/")
def index() -> FileResponse:
    return FileResponse(_UI_FILE, media_type="text/html")


@app.get("/ui/{name}")
def ui_asset(name: str) -> FileResponse:
    if name not in _UI_ASSETS:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(_UI_FILE.parent / name, media_type=_UI_ASSETS[name])


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/ready")
def ready(request: Request) -> Response:
    """Readiness: the database answers and the search index is built. 503 while not ready."""
    ws = getattr(request.app.state, "ws", None)
    checks: dict[str, Any] = {}
    try:
        ws.store.run_select("SELECT 1 AS ok", max_rows=1)
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "error"
    checks["index"] = "ok" if ws and ws.last_reindex else "building"
    checks["ai_model"] = "available" if ws and ws.router.available else "none (extractive mode)"
    ok = checks["database"] == "ok" and checks["index"] == "ok"
    return JSONResponse({"ready": ok, "checks": checks}, status_code=200 if ok else 503)


@app.get("/metrics")
def metrics(request: Request) -> Response:
    """Prometheus text format: HTTP, model calls/tokens/cost, plus live gauges."""
    ws = _ws(request)
    if not ws.settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="not found")
    gauges = {
        "assistant_documents": (len(ws.engine.documents), "Documents indexed"),
        "assistant_search_chunks": (len(ws.engine.retriever.chunks), "Passages in the search index"),
        "assistant_invoices": (len(ws.invoices.records), "Invoices extracted"),
        "assistant_invoices_needs_review": (sum(r.status == "needs_review" for r in ws.invoices.records),
                                            "Invoices awaiting review"),
        "assistant_approvals_pending": (len(ws.approvals.list("pending")), "Actions waiting for approval"),
        "assistant_models_available": (len(ws.router.models()), "Configured AI models"),
    }
    return PlainTextResponse(METRICS.render(gauges), media_type="text/plain; version=0.0.4")


@app.get("/health")
def health(request: Request) -> dict:
    ws = _ws(request)
    return {
        "status": "ok",
        "version": __version__,
        "public_demo": ws.settings.public_demo,
        "config_warnings": ws.config_warnings,
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
            "What needs my attention this week?",
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


class ExtractIn(BaseModel):
    filename: str = Field(default="pasted.txt", max_length=255)
    text: str = Field(min_length=1, max_length=100_000)


@app.post("/extract")
def extract(body: ExtractIn, request: Request) -> dict:
    """Invoice lab: read invoice fields from pasted text with the local rules (no AI model, nothing
    stored). Every field comes with the line it was read from; anything missing or doubtful is flagged."""
    ws = _ws(request)
    ex = extract_invoice(body.text, known_suppliers=ws._known_vendors(), date_order=ws.settings.date_order)
    fields = {name: {"value": fv.value, "confidence": round(fv.confidence, 2), "evidence": fv.evidence, "note": fv.note}
              for name, fv in ex.fields.items()}
    ws.audit.record("invoice.extract_preview", actor=_actor(request), chars=len(body.text))
    return {"filename": body.filename, "fields": fields, "confidence": round(ex.confidence, 2),
            "issues": ex.issues, "line_items": ex.line_items, "method": "rules (no AI, nothing stored)"}


@app.post("/chat")
def chat(body: ChatIn, request: Request) -> dict:
    return _ws(request).engine.answer(body.session_id, body.question.strip(), actor=_actor(request))


@app.post("/chat/stream")
def chat_stream(body: ChatIn, request: Request) -> StreamingResponse:
    """Same as /chat, streamed as server-sent events (progress, scrubbed text snapshots, final payload)."""
    engine = _ws(request).engine
    events = stream_answer(engine, body.session_id, body.question.strip(), actor=_actor(request))

    def sse():
        for ev in events:
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    with ws._lock:  # reindex() and sync_qbo() rebuild the same tables
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
    with ws._lock:
        for table in [t for t in ws.store.tables() if t.startswith("qbo_") or t in ("invoice_reconciliation", "bank_reconciliation")]:
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


@app.get("/dashboard")
def dashboard(request: Request) -> dict:
    """KPIs and chart series (aging, spend, cash flow, reconciliation, budget, extraction confidence, models)."""
    return _ws(request).dashboard()


@app.get("/insights")
def get_insights(request: Request) -> dict:
    """Everything that needs attention, ranked by severity and money involved, each with its source."""
    return _ws(request).attention()


@app.get("/deadlines")
def get_deadlines(request: Request) -> dict:
    """Dated obligations read from the documents (renewals, expiries, notices, inspections, milestones)."""
    a = _ws(request).attention()
    return {"as_of": a["as_of"], "deadlines": a["deadlines"]}


@app.get("/bank-reconciliation")
def bank_reconciliation(request: Request) -> dict:
    ws = _ws(request)
    if "bank_reconciliation" not in ws.store.tables():
        return {"rows": []}
    cols, rows = ws.store.run_select(
        "SELECT * FROM bank_reconciliation ORDER BY CASE severity WHEN 'issue' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, "
        "date", max_rows=5000)
    return {"rows": [dict(zip(cols, r, strict=False)) for r in rows]}


@app.get("/reports")
def list_reports() -> dict:
    return {"reports": [{"id": k, "title": v["title"], "description": v["description"]} for k, v in REPORTS.items()]}


@app.get("/reports/{report_id}.md")
def report_markdown(report_id: str, request: Request) -> PlainTextResponse:
    rep = _report_or_404(request, report_id)
    return PlainTextResponse(rep["markdown"], media_type="text/markdown",
                             headers={"Content-Disposition": f'attachment; filename="{report_id}-report-draft.md"'})


@app.get("/reports/{report_id}.html")
def report_html(report_id: str, request: Request) -> Response:
    rep = _report_or_404(request, report_id)
    return Response(markdown_to_html(rep["markdown"], rep["title"]), media_type="text/html")


@app.get("/reports/{report_id}")
def report_json(report_id: str, request: Request) -> dict:
    return _report_or_404(request, report_id)


@app.post("/reports/{report_id}/summary")
def report_summary(report_id: str, request: Request) -> dict:
    _report_or_404(request, report_id)
    return _ws(request).report_summary(report_id, actor=_actor(request))


def _report_or_404(request: Request, report_id: str) -> dict:
    try:
        return _ws(request).report(report_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="unknown report") from exc


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
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Log the full error server-side (with the request ID); never leak internals to the client.
    log.error("unhandled_error", exc_info=exc, extra={"fields": {"path": request.url.path}})
    return JSONResponse({"error": "internal error", "request_id": request_id_var.get()}, status_code=500)

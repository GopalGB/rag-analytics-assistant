"""FastAPI entrypoint. Run with: uvicorn app.main:app --host 127.0.0.1 --port 8000"""

from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.agent.engine import AgentEngine
from app.agent.llm import ProviderRateLimitError, build_llm
from app.agent.memory import ConversationMemory
from app.config import Settings
from app.data import ingest, qbo, watcher
from app.data.store import DataStore
from app.middleware import install_security_middleware
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Retriever
from app.security import InputGuard

_UI_FILE = Path(__file__).parent / "ui" / "chat.html"


class LLMNotConfiguredError(RuntimeError):
    """Raised at startup when require_llm is set but no provider is configured."""


def build_engine(settings: Settings) -> AgentEngine:
    llm = build_llm(settings)
    if llm is None and settings.require_llm:
        raise LLMNotConfiguredError(
            "This is an LLM-first assistant and no model is configured. Set ONE of:\n"
            "  • OPENAI_API_KEY   (OpenAI or any compatible /chat/completions endpoint)\n"
            "  • BEDROCK_MODEL_ID (AWS Bedrock; pip install boto3 + AWS credentials)\n"
            "  • LLM_CLI_COMMAND  (a local CLI, e.g. the ChatGPT/Codex CLI on your subscription)\n"
            "Then restart. To run without a model anyway (it will refuse to answer), set "
            "REQUIRE_LLM=false."
        )
    store = DataStore(settings.db_path)
    ingest.load_tables(store, settings.data_dir)
    embeddings = EmbeddingService(
        provider=settings.embedding_provider,
        dim=settings.local_embedding_dim,
        openai_api_key=settings.openai_api_key,
        openai_base_url=settings.openai_base_url,
        openai_model=settings.openai_embedding_model,
    )
    retriever = Retriever(embeddings).build(ingest.load_chunks(settings.data_dir))
    guard = InputGuard(max_input_chars=settings.max_input_chars)
    memory = ConversationMemory(max_turns=settings.history_turns)
    return AgentEngine(
        store=store,
        retriever=retriever,
        guard=guard,
        llm=llm,
        memory=memory,
        invoice_records=ingest.invoice_records(settings.data_dir),
        max_tool_iterations=settings.max_tool_iterations,
        max_sql_rows=settings.max_sql_rows,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings
    app.state.engine = build_engine(settings)
    # Auto-ingest: watch the data dir and re-embed/re-index new or changed files on the fly.
    app.state.watch_stop = asyncio.Event()
    app.state.watch_task = None
    if settings.auto_reindex:
        app.state.watch_task = asyncio.create_task(
            watcher.run_watcher(
                app.state.engine,
                settings.data_dir,
                settings.reindex_interval_seconds,
                app.state.watch_stop,
            )
        )
    try:
        yield
    finally:
        if app.state.watch_task is not None:
            app.state.watch_stop.set()
            app.state.watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await app.state.watch_task
        app.state.engine.store.close()


app = FastAPI(
    title="RAG Analytics Assistant",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
install_security_middleware(app, Settings())


class ChatIn(BaseModel):
    question: str = Field(default="", max_length=8000)
    session_id: str = Field(default="default", max_length=128)


class ExtractIn(BaseModel):
    filename: str = Field(default="upload.txt", max_length=255)
    text: str = Field(min_length=1, max_length=100_000)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_UI_FILE, media_type="text/html")


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/health")
def health(request: Request) -> dict:
    settings: Settings = request.app.state.settings
    llm = request.app.state.engine.llm
    provider = type(llm).__name__.removesuffix("LLM").lower() if llm else "none"
    provider = "cli" if provider == "command" else provider
    model = getattr(llm, "model", getattr(llm, "model_id", None)) if llm else None
    quickbooks = "disabled" if settings.public_demo else qbo.QBOSandboxClient(
        settings.qbo_sandbox_access_token, settings.qbo_realm_id
    ).status
    return {
        "status": "ok",
        "provider": provider,
        "model": model,
        "retrieval": "lexical/hash",
        "public_demo": settings.public_demo,
        "quickbooks": quickbooks,
        **request.app.state.engine.status(),
    }


@app.get("/architecture")
def architecture(request: Request) -> dict:
    llm = request.app.state.engine.llm
    provider = type(llm).__name__.removesuffix("LLM").lower() if llm else "none"
    return {
        "provider": "cli" if provider == "command" else provider,
        "model": getattr(llm, "model", getattr(llm, "model_id", None)) if llm else None,
        "retrieval": "lexical/hash",
        "storage": "local DuckDB",
        "public_demo": request.app.state.settings.public_demo,
    }


@app.get("/documents")
def documents(request: Request) -> dict:
    data_dir = Path(request.app.state.settings.data_dir).resolve()
    items = []
    for path in ingest.source_paths(data_dir):
        if path.suffix.lower() in ingest._DOC_SUFFIXES | ingest._TABLE_SUFFIXES:
            items.append(
                {
                    "id": path.name,
                    "name": path.name,
                    "title": path.stem,
                    "type": path.suffix.lower().lstrip("."),
                    "bytes": path.stat().st_size,
                    "chunks": sum(
                        1 for c in request.app.state.engine.retriever.chunks if c.file == path.name
                    ),
                    "url": f"/documents/{quote(path.name, safe='')}",
                }
            )
    return {
        "documents": items,
        "counts": {"documents": len(items), "chunks": len(request.app.state.engine.retriever.chunks)},
    }


@app.get("/documents/{filename:path}")
def document(filename: str, request: Request) -> FileResponse:
    sources = {path.name: path for path in ingest.source_paths(Path(request.app.state.settings.data_dir))}
    target = sources.get(filename)
    if target is None:
        raise HTTPException(status_code=404, detail="document not found")
    return FileResponse(target)


@app.get("/invoices")
def invoices(request: Request) -> dict:
    rows = []
    for record in request.app.state.engine.invoice_records:
        parsed = dict(record)
        parsed["source_url"] = f"/documents/{quote(parsed['source_file'], safe='')}"
        rows.append(parsed)
    return {"invoices": rows, "count": len(rows)}


@app.post("/extract")
def extract(body: ExtractIn) -> dict:
    return {"invoice": ingest.extract_invoice(body.text, body.filename), "method": "deterministic-regex"}


@app.get("/examples")
def examples() -> dict:
    return {
        "examples": [
            "What columns are in the data?",
            "What were total sales by region?",
            "Which 5 products had the highest revenue?",
            "Summarize the onboarding document.",
        ]
    }


@app.post("/chat")
def chat(body: ChatIn, request: Request) -> dict:
    started = time.perf_counter()
    public_demo = request.app.state.settings.public_demo
    result = request.app.state.engine.answer(body.session_id, body.question.strip(), remember=not public_demo)
    result.setdefault("timings_ms", {})["total"] = round((time.perf_counter() - started) * 1000, 2)
    sources = {path.name for path in ingest.source_paths(Path(request.app.state.settings.data_dir))}
    safe_sources = [
        {**source, "url": f"/documents/{quote(source['file'], safe='')}"}
        for source in result.get("sources", [])
        if source.get("file") in sources
    ]
    if result.get("route") == "agent" and not safe_sources:
        result["route"] = "abstained"
        result["text"] = "I don't have a current corpus source for that answer. Ask about the loaded documents or tables."
        result["sql"] = None
        result["columns"] = []
        result["rows"] = []
        result["row_count"] = 0
    result["sources"] = safe_sources
    return result


def _qbo_client(settings: Settings) -> qbo.QBOSandboxClient:
    return qbo.QBOSandboxClient(settings.qbo_sandbox_access_token, settings.qbo_realm_id)


@app.get("/integrations/quickbooks/status")
def quickbooks_status(request: Request) -> dict:
    if request.app.state.settings.public_demo:
        return {"status": "disabled", "message": "QuickBooks is disabled in the public demo."}
    status = _qbo_client(request.app.state.settings).status
    return {"status": status, "message": "QuickBooks is not configured." if status == "not_configured" else "QuickBooks sandbox configured."}


def _qbo_records(request: Request, entity: str) -> dict:
    if request.app.state.settings.public_demo:
        raise HTTPException(status_code=404, detail="QuickBooks is disabled in the public demo")
    client = _qbo_client(request.app.state.settings)
    if client.status != "configured":
        return {"status": "not_configured", entity.lower(): []}
    records = client.list_invoices() if entity == "Invoice" else client.list_vendors()
    return {"status": "configured", entity.lower(): records}


@app.get("/integrations/quickbooks/invoices")
def quickbooks_invoices(request: Request) -> dict:
    return _qbo_records(request, "Invoice")


@app.get("/integrations/quickbooks/vendors")
def quickbooks_vendors(request: Request) -> dict:
    return _qbo_records(request, "Vendor")


@app.post("/refresh")
def refresh(request: Request) -> dict:
    # Manual trigger for the same reindex the auto-watcher runs (reload tables, re-embed docs).
    settings: Settings = request.app.state.settings
    if settings.public_demo:
        raise HTTPException(status_code=404, detail="refresh disabled in public demo")
    engine: AgentEngine = request.app.state.engine
    result = watcher.reindex(engine, settings.data_dir)
    return {"status": "refreshed", **result}


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    # Never leak internals to the client.
    return JSONResponse({"error": "internal error"}, status_code=500)


@app.exception_handler(ProviderRateLimitError)
async def _provider_rate_limited(_: Request, exc: ProviderRateLimitError) -> JSONResponse:
    headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
    return JSONResponse(
        {"detail": "The model provider is temporarily rate limited. Please retry shortly."},
        status_code=429,
        headers=headers,
    )

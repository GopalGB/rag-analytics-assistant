"""FastAPI entrypoint. Run with: uvicorn app.main:app --host 127.0.0.1 --port 8000"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.agent.engine import AgentEngine
from app.agent.llm import build_llm
from app.agent.memory import ConversationMemory
from app.config import Settings
from app.data import ingest, watcher
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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_UI_FILE, media_type="text/html")


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/health")
def health(request: Request) -> dict:
    return {"status": "ok", **request.app.state.engine.status()}


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
    return request.app.state.engine.answer(body.session_id, body.question.strip())


@app.post("/refresh")
def refresh(request: Request) -> dict:
    # Manual trigger for the same reindex the auto-watcher runs (reload tables, re-embed docs).
    settings: Settings = request.app.state.settings
    engine: AgentEngine = request.app.state.engine
    result = watcher.reindex(engine, settings.data_dir)
    return {"status": "refreshed", **result}


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    # Never leak internals to the client.
    return JSONResponse({"error": "internal error"}, status_code=500)

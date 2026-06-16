"""FastAPI entrypoint. Run with: uvicorn app.main:app --host 127.0.0.1 --port 8000"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.agent.engine import AgentEngine
from app.agent.llm import build_llm
from app.agent.memory import ConversationMemory
from app.config import Settings
from app.data import ingest
from app.data.store import DataStore
from app.middleware import install_security_middleware
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Retriever
from app.security import InputGuard

_UI_FILE = Path(__file__).parent / "ui" / "chat.html"


def build_engine(settings: Settings) -> AgentEngine:
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
        llm=build_llm(settings),
        memory=memory,
        max_tool_iterations=settings.max_tool_iterations,
        max_sql_rows=settings.max_sql_rows,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings
    app.state.engine = build_engine(settings)
    try:
        yield
    finally:
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
    settings: Settings = request.app.state.settings
    engine: AgentEngine = request.app.state.engine
    loaded = ingest.load_tables(engine.store, settings.data_dir)
    # Build a fresh retriever off to the side, then swap the reference atomically so a concurrent
    # /chat never observes a half-rebuilt index.
    new_retriever = Retriever(engine.retriever.embeddings).build(ingest.load_chunks(settings.data_dir))
    engine.retriever = new_retriever
    return {
        "status": "refreshed",
        "tables": loaded,
        "doc_chunks": len(new_retriever.chunks),
    }


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    # Never leak internals to the client.
    return JSONResponse({"error": "internal error"}, status_code=500)

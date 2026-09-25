"""Auto-ingest: keep the live store/retriever in sync with the data directory.

Drop a spreadsheet, document, scan, or invoice anywhere under the data dir and it is parsed (OCR for
scans), embedded, and indexed automatically — no restart, no manual refresh. A background poller
compares a cheap signature of the directory (path + size + mtime per file) each tick and rebuilds only
when something actually changed.

`reindex` is the shared core; the workspace wraps it (adding invoice extraction and reconciliation),
and both the watcher and the manual `/refresh` endpoint go through the workspace.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING

from app.data import ingest
from app.documents.ocr import OCREngine
from app.documents.parsers import DOC_SUFFIXES, ParseCache
from app.rag.retriever import Retriever

if TYPE_CHECKING:  # avoid an import cycle at runtime; only needed for type hints
    from app.agent.engine import AgentEngine

WATCHED_SUFFIXES = DOC_SUFFIXES | {".csv", ".xlsx"}

# Signature of the data dir: a sorted tuple of (relative path, size, mtime_ns) for every watched file.
DirSignature = tuple[tuple[str, int, int], ...]


def dir_signature(data_dir: str) -> DirSignature:
    entries: list[tuple[str, int, int]] = []
    for path, rel in ingest.iter_files(data_dir, WATCHED_SUFFIXES):
        st = path.stat()
        entries.append((rel, st.st_size, st.st_mtime_ns))
    return tuple(entries)


def reindex(
    engine: AgentEngine, data_dir: str, ocr: OCREngine | None = None, cache: ParseCache | None = None
) -> dict:
    """(Re)load all tables and documents from `data_dir`, dropping tables whose file is gone.

    Tables are loaded with an atomic temp-table swap (see DataStore.load_dataframe); the retriever is
    rebuilt off to the side and the reference is swapped in one assignment, so a concurrent /chat
    never observes a half-built index. Only file-backed tables are ever dropped — tables the app
    manages (extracted invoices, QuickBooks data) are left alone."""
    previous = set(engine.store.file_tables)
    loaded = ingest.load_tables(engine.store, data_dir)
    for table in previous - engine.store.file_tables:
        engine.store.drop_table(table)
    docs = ingest.load_documents(data_dir, ocr, cache)
    old = engine.retriever
    new_retriever = Retriever(old.embeddings, old._fixed_weight, old.mmr_lambda)
    new_retriever.reranker = old.reranker
    new_retriever.build(ingest.chunk_documents(docs))
    engine.retriever = new_retriever
    engine.documents = docs
    return {"tables": loaded, "documents": len(docs), "doc_chunks": len(new_retriever.chunks),
            "failed_tables": sorted(engine.store.failed_tables)}


MAX_FAILED_RETRIES = 3  # full rebuilds for the same folder state while a spreadsheet stays unreadable


async def run_watcher(
    reindex_fn: Callable[[], object], data_dir: str, interval_seconds: int, stop: asyncio.Event
) -> None:
    """Poll the data dir until `stop` is set, calling `reindex_fn` whenever its signature changes.

    The startup ingest has already run, so we seed the signature with the current state and only act
    on later changes. Reindexing runs in a worker thread so the event loop stays responsive, and the
    new signature is committed only after a successful rebuild — a file caught mid-write simply errors
    this tick and is retried on the next one, up to MAX_FAILED_RETRIES times for the same folder state."""
    last_sig = dir_signature(data_dir)
    retried: tuple[object, int] = (None, 0)  # (signature, failed attempts) for a spreadsheet that won't load
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        if stop.is_set():
            break
        try:
            sig = dir_signature(data_dir)
        except Exception:
            continue  # the folder is being changed right now: look again next tick
        if sig == last_sig:
            continue
        try:
            result = await asyncio.to_thread(reindex_fn)
            failed = isinstance(result, dict) and bool(result.get("failed_tables"))
        except Exception:
            failed = True  # transient (e.g. a file mid-copy)
        if failed:
            attempts = retried[1] + 1 if retried[0] == sig else 1
            retried = (sig, attempts)
            if attempts < MAX_FAILED_RETRIES:
                continue  # keep last_sig so the next tick retries
            # still failing: stop rebuilding every tick and wait for the folder to change again
        retried = (None, 0)
        last_sig = sig

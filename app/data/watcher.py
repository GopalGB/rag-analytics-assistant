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
    for table in previous - set(loaded):
        engine.store.drop_table(table)
    docs = ingest.load_documents(data_dir, ocr, cache)
    new_retriever = Retriever(engine.retriever.embeddings).build(ingest.chunk_documents(docs))
    engine.retriever = new_retriever
    engine.documents = docs
    return {"tables": loaded, "documents": len(docs), "doc_chunks": len(new_retriever.chunks)}


async def run_watcher(
    reindex_fn: Callable[[], object], data_dir: str, interval_seconds: int, stop: asyncio.Event
) -> None:
    """Poll the data dir until `stop` is set, calling `reindex_fn` whenever its signature changes.

    The startup ingest has already run, so we seed the signature with the current state and only act
    on later changes. Reindexing runs in a worker thread so the event loop stays responsive, and the
    new signature is committed only after a successful rebuild — a file caught mid-write simply errors
    this tick and is retried on the next one."""
    last_sig = dir_signature(data_dir)
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        if stop.is_set():
            break
        try:
            sig = dir_signature(data_dir)
            if sig != last_sig:
                await asyncio.to_thread(reindex_fn)
                last_sig = sig
        except Exception:
            # Transient (e.g. a file mid-copy). Leave last_sig unchanged so we retry next tick.
            continue

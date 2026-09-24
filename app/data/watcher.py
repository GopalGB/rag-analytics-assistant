"""Auto-ingest: keep the live store/retriever in sync with the data directory.

Drop a CSV (table) or .md/.txt (document) into the data dir and it is loaded, embedded, and indexed
automatically — no restart, no manual refresh. A background poller compares a cheap signature of the
directory (name + size + mtime per file) each tick and rebuilds only when something actually changed.

`reindex` is also what the manual `/refresh` endpoint calls, so both paths share one code path.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from app.data import ingest
from app.rag.retriever import Retriever

if TYPE_CHECKING:  # avoid an import cycle at runtime; only needed for type hints
    from app.agent.engine import AgentEngine

_WATCHED_SUFFIXES = {".csv", ".md", ".txt"}

# Signature of the data dir: a sorted tuple of (name, size, mtime_ns) for every watched file.
DirSignature = tuple[tuple[str, int, int], ...]


def dir_signature(data_dir: str) -> DirSignature:
    entries: list[tuple[str, int, int]] = []
    for path in sorted(Path(data_dir).glob("*")):
        if path.suffix.lower() not in _WATCHED_SUFFIXES or not path.is_file():
            continue
        st = path.stat()
        entries.append((path.name, st.st_size, st.st_mtime_ns))
    return tuple(entries)


def reindex(engine: AgentEngine, data_dir: str) -> dict:
    """(Re)load all tables and documents from `data_dir`, dropping tables whose file is gone.

    Tables are loaded with an atomic temp-table swap (see DataStore.load_dataframe); the retriever is
    rebuilt off to the side and the reference is swapped in one assignment, so a concurrent /chat
    never observes a half-built index. DuckDB access is serialized by the store's internal lock."""
    loaded = ingest.load_tables(engine.store, data_dir)
    for table in engine.store.tables():
        if table not in loaded:
            engine.store.drop_table(table)
    chunks = []
    errors = []
    for path in ingest.source_paths(Path(data_dir), ingest._DOC_SUFFIXES):
        try:
            for i, piece in enumerate(ingest.chunk_text(ingest.read_document(path))):
                from app.rag.retriever import Chunk

                chunks.append(Chunk(file=path.name, chunk_id=i, text=piece))
        except ingest.DocumentReadError as exc:
            errors.append({"file": path.name, "error": str(exc)})
    new_retriever = Retriever(engine.retriever.embeddings).build(chunks)
    engine.retriever = new_retriever
    engine.invoice_records = ingest.invoice_records(data_dir)
    return {"tables": loaded, "doc_chunks": len(new_retriever.chunks), "errors": errors}


async def run_watcher(engine: AgentEngine, data_dir: str, interval_seconds: int, stop: asyncio.Event) -> None:
    """Poll the data dir until `stop` is set, reindexing whenever its signature changes.

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
                await asyncio.to_thread(reindex, engine, data_dir)
                last_sig = sig
        except Exception:
            # Transient (e.g. a file mid-copy). Leave last_sig unchanged so we retry next tick.
            continue

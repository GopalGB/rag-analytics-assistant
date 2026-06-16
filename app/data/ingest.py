"""Ingestion: load CSVs into DuckDB tables and split documents into retrievable chunks."""

from __future__ import annotations

from pathlib import Path

from app.data.store import DataStore
from app.rag.retriever import Chunk

_DOC_SUFFIXES = {".md", ".txt"}
_TABLE_SUFFIXES = {".csv"}


def _safe_table_name(stem: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in stem.lower()).strip("_")
    return cleaned or "table"


def load_tables(store: DataStore, data_dir: str) -> dict[str, int]:
    """Load every CSV in `data_dir` as a table named after the file stem. Returns {table: rows}."""
    loaded: dict[str, int] = {}
    for path in sorted(Path(data_dir).glob("*")):
        if path.suffix.lower() in _TABLE_SUFFIXES:
            table = _safe_table_name(path.stem)
            loaded[table] = store.load_csv(table, str(path))
    return loaded


def chunk_text(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def load_chunks(data_dir: str, size: int = 800, overlap: int = 150) -> list[Chunk]:
    """Read .md/.txt documents and split them into overlapping chunks."""
    chunks: list[Chunk] = []
    for path in sorted(Path(data_dir).glob("*")):
        if path.suffix.lower() not in _DOC_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for i, piece in enumerate(chunk_text(text, size, overlap)):
            chunks.append(Chunk(file=path.name, chunk_id=i, text=piece))
    return chunks

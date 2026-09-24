"""Ingestion: load spreadsheets into DuckDB tables and split documents into page-aware chunks.

The data dir is scanned recursively (hidden files/dirs are skipped):
- .csv / .xlsx  -> a SQL table named after the file stem
- .pdf / .docx / .md / .txt / images -> parsed (OCR for scans) and chunked for retrieval
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from app.data.store import DataStore
from app.documents.ocr import OCREngine
from app.documents.parsers import DOC_SUFFIXES, ParseCache, ParsedDocument
from app.rag.retriever import Chunk

_TABLE_SUFFIXES = {".csv", ".xlsx"}
# Tables the app manages itself; a spreadsheet with the same name is loaded under a prefixed name.
RESERVED_TABLES = {"invoices", "invoice_reconciliation", "invoice_lines", "bank_reconciliation"}
RESERVED_PREFIXES = ("qbo_",)


def iter_files(data_dir: str, suffixes: set[str]) -> Iterator[tuple[Path, str]]:
    """Yield (path, relative posix path) for matching files, recursively, skipping hidden entries."""
    root = Path(data_dir)
    if not root.exists():
        return
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts) or not path.is_file():
            continue
        if path.suffix.lower() in suffixes:
            yield path, "/".join(rel_parts)


def _safe_table_name(stem: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in stem.lower()).strip("_")
    cleaned = cleaned or "table"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    if cleaned in RESERVED_TABLES or cleaned.startswith(RESERVED_PREFIXES):
        cleaned = f"file_{cleaned}"
    return cleaned


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".xlsx":
        return pd.read_excel(path)  # first sheet
    return pd.read_csv(path)


def load_tables(store: DataStore, data_dir: str) -> dict[str, int]:
    """Load every CSV/XLSX under `data_dir` as a table named after the file stem. Returns {table: rows}."""
    loaded: dict[str, int] = {}
    failed: set[str] = set()
    for path, rel in iter_files(data_dir, _TABLE_SUFFIXES):
        table = _unique_table_name(path, rel, loaded.keys() | failed)
        try:
            loaded[table] = store.load_dataframe(table, _read_table(path))
        except Exception:
            # Unreadable right now (e.g. mid-copy): keep the previous table, and report it so the watcher
            # retries even if the folder doesn't change again.
            failed.add(table)
    store.file_tables = set(loaded) | (failed & store.file_tables)
    store.failed_tables = failed
    return loaded


def _unique_table_name(path: Path, rel: str, taken: set[str]) -> str:
    """Table name from the file name; if another spreadsheet already has it (same name in another folder,
    or `invoice_lines.csv` next to `file_invoice_lines.csv`), use the relative path, then a number."""
    name = _safe_table_name(path.stem)
    if name in taken:
        name = _safe_table_name(rel.rsplit(".", 1)[0])
    base, n = name, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def load_documents(data_dir: str, ocr: OCREngine | None = None, cache: ParseCache | None = None) -> list[ParsedDocument]:
    ocr = ocr or OCREngine()
    cache = cache or ParseCache(None)
    return [cache.parse(path, rel, ocr) for path, rel in iter_files(data_dir, DOC_SUFFIXES)]


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


def chunk_documents(docs: list[ParsedDocument], size: int = 800, overlap: int = 150) -> list[Chunk]:
    """Split each page separately so every chunk can cite its page number."""
    chunks: list[Chunk] = []
    for doc in docs:
        cid = 0
        paged = doc.kind == "pdf"
        for pno, page in enumerate(doc.pages, start=1):
            for piece in chunk_text(page, size, overlap):
                chunks.append(Chunk(file=doc.file, chunk_id=cid, text=piece, page=pno if paged else None))
                cid += 1
    return chunks


def load_chunks(data_dir: str, size: int = 800, overlap: int = 150) -> list[Chunk]:
    """Parse all documents under `data_dir` and split them into overlapping, page-aware chunks."""
    return chunk_documents(load_documents(data_dir), size, overlap)

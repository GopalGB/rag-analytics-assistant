"""Ingestion: load CSVs into DuckDB tables and split documents into retrievable chunks."""

from __future__ import annotations

import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree

from app.data.store import DataStore
from app.rag.retriever import Chunk

_DOC_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}
_TABLE_SUFFIXES = {".csv"}
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 50


class DocumentReadError(ValueError):
    """A supported source could not be safely read."""


def _safe_table_name(stem: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in stem.lower()).strip("_")
    return cleaned or "table"


def load_tables(store: DataStore, data_dir: str) -> dict[str, int]:
    """Load every CSV in `data_dir` as a table named after the file stem. Returns {table: rows}."""
    loaded: dict[str, int] = {}
    for path in source_paths(Path(data_dir), _TABLE_SUFFIXES):
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
    for path in source_paths(Path(data_dir), _DOC_SUFFIXES):
        if path.suffix.lower() not in _DOC_SUFFIXES:
            continue
        text = read_document(path)
        for i, piece in enumerate(chunk_text(text, size, overlap)):
            chunks.append(Chunk(file=path.name, chunk_id=i, text=piece))
    return chunks


def invoice_records(data_dir: str) -> list[dict]:
    """Extract invoice-like source files once for the API and the read-only agent tool."""
    records = []
    for path in source_paths(Path(data_dir), _DOC_SUFFIXES):
        try:
            record = extract_invoice(read_document(path), path.name)
        except DocumentReadError:
            continue
        if any(record.get(key) for key in ("supplier", "invoice_number", "amount")):
            record["source_file"] = path.name
            records.append(record)
    return records


def read_document(path: Path) -> str:
    """Read supported local documents without persisting extracted content."""
    path = Path(path)
    if not path.exists() or path.is_symlink() or not path.is_file():
        raise DocumentReadError(f"unsafe or missing source: {path.name}")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise DocumentReadError(f"source too large: {path.name}")
    suffix = path.suffix.lower()
    if suffix in {".md", ".txt"}:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DocumentReadError(f"unable to read {path.name}") from exc
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as archive:
                xml = archive.read("word/document.xml")
            root = ElementTree.fromstring(xml)
            return " ".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
        except (OSError, KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            raise DocumentReadError(f"malformed DOCX: {path.name}") from exc
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            pages = PdfReader(str(path)).pages
            if len(pages) > MAX_PDF_PAGES:
                raise DocumentReadError(f"PDF has too many pages: {path.name}")
            text = "\n".join(page.extract_text() or "" for page in pages)
            if not text.strip():
                raise DocumentReadError(f"PDF contains no extractable text: {path.name}")
            return text
        except DocumentReadError:
            raise
        except (ImportError, OSError, ValueError) as exc:
            raise DocumentReadError(f"malformed PDF: {path.name}") from exc
    raise DocumentReadError(f"unsupported source: {path.name}")


def source_paths(data_dir: Path, suffixes: set[str] | None = None) -> list[Path]:
    """List only direct, regular files under the configured root."""
    root = Path(data_dir).resolve()
    if not root.exists() or not root.is_dir():
        return []
    allowed = suffixes or (_DOC_SUFFIXES | _TABLE_SUFFIXES)
    return [
        p
        for p in sorted(root.iterdir())
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in allowed
    ]


def extract_invoice(text: str, filename: str = "upload") -> dict[str, str | list[str] | None | dict]:
    """Deterministic extraction for demo data; missing fields remain explicit."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    evidence: dict[str, str] = {}
    review: set[str] = set()

    def labeled(label: str) -> list[str]:
        return [line.split(":", 1)[1].strip() for line in lines if re.match(rf"^{re.escape(label)}\s*:", line, re.I)]

    def one(field: str, labels: tuple[str, ...]) -> str | None:
        values = [v for label in labels for v in labeled(label)]
        if len(values) > 1:
            review.add(field)
            evidence[field] = " | ".join(values)
            return None
        if values:
            evidence[field] = values[0]
            return values[0]
        return None

    supplier = one("supplier", ("supplier", "vendor"))
    number = one("invoice_number", ("invoice number", "invoice no."))
    if number is None:
        matches = [match.group(1).strip() for line in lines if (match := re.match(r"^invoice\s+([^:]+)$", line, re.I))]
        if len(matches) == 1:
            number = matches[0]
            evidence["invoice_number"] = number
        elif len(matches) > 1:
            review.add("invoice_number")
            evidence["invoice_number"] = " | ".join(matches)
    raw_date = one("date", ("date", "invoice date"))
    parsed_date = None
    if raw_date:
        try:
            parsed_date = date.fromisoformat(raw_date).isoformat()
        except ValueError:
            review.add("date")
    money: dict[str, str | None] = {}

    def decimal_money(raw: str) -> str | None:
        match = re.fullmatch(
            r"\s*(?:(AED|USD|EUR|GBP)\s*|\$\s*)?([\d,]+(?:\.\d{1,2})?)(?:\s*(AED|USD|EUR|GBP))?\s*",
            raw,
            re.I,
        )
        try:
            return f"{Decimal(match.group(2).replace(',', '')):.2f}" if match else None
        except InvalidOperation:
            return None

    for field, labels in (
        ("subtotal", ("subtotal",)),
        ("tax", ("tax",)),
        ("amount", ("grand total", "total", "amount due", "amount")),
    ):
        vals = [v for label in labels for v in labeled(label)]
        if len(vals) > 1:
            review.add(field)
            money[field] = None
            continue
        raw = vals[0] if vals else None
        if raw:
            evidence[field] = raw
        money[field] = decimal_money(raw) if raw else None
        if raw and money[field] is None:
            review.add(field)
    currencies = [m.upper() for v in evidence.values() for m in re.findall(r"\b(AED|USD|EUR|GBP)\b", v, re.I)]
    raw_currency = one("currency", ("currency",))
    if raw_currency and raw_currency.upper() in {"AED", "USD", "EUR", "GBP"}:
        currency = raw_currency.upper()
    else:
        currency = currencies[0] if len(set(currencies)) == 1 else None
    if raw_currency and currency is None:
        review.add("currency")
    if any("$" in value for value in evidence.values()) and not raw_currency:
        review.add("currency")
    if len(set(currencies)) > 1 or (currency and any(code != currency for code in currencies)):
        review.add("currency")
    subtotal, tax, amount = money["subtotal"], money["tax"], money["amount"]
    if subtotal and tax and amount and Decimal(subtotal) + Decimal(tax) != Decimal(amount):
        review.add("amount")
    quantity = one("quantity", ("quantity",))
    unit_price = one("unit_price", ("unit price",))
    if quantity and unit_price and amount:
        try:
            if Decimal(quantity) * Decimal(decimal_money(unit_price) or "NaN") != Decimal(amount):
                review.add("amount")
        except InvalidOperation:
            review.add("amount")
    values = {
        "supplier": supplier,
        "invoice_number": number,
        "date": parsed_date,
        "amount": amount,
        "currency": currency,
        "subtotal": subtotal,
        "tax": tax,
        "filename": filename,
    }
    values["missing_fields"] = [
        k for k in ("supplier", "invoice_number", "date", "amount", "currency") if not values[k]
    ]
    values["review_fields"] = sorted(review)
    values["evidence"] = evidence
    return values

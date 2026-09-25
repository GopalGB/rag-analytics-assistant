"""Turn files into page-aware text, locally.

Supported: .pdf (text layer, or local OCR for scanned pages), .docx, .md/.txt, and images
(.png/.jpg/.jpeg/.tif/.tiff via OCR). Spreadsheets (.csv/.xlsx) are loaded as SQL tables elsewhere.

Every parse records `warnings` (e.g. "page 1 is scanned and OCR is not installed") so downstream
steps can flag uncertain documents instead of guessing. Results are cached by content hash, so
unchanged files are never re-OCR'd on reindex.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.documents.ocr import OCREngine

PARSER_VERSION = "1"

DOC_SUFFIXES = {".pdf", ".docx", ".md", ".txt", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}
_MIN_TEXT_CHARS = 25  # a PDF page with less text than this is treated as scanned


@dataclass
class ParsedDocument:
    file: str  # path relative to the data dir, posix style
    sha256: str
    kind: str  # pdf | docx | text | image
    pages: list[str] = field(default_factory=list)
    ocr_pages: list[int] = field(default_factory=list)  # 1-based page numbers that came from OCR
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(self.pages)

    @property
    def used_ocr(self) -> bool:
        return bool(self.ocr_pages)

    def to_dict(self) -> dict:
        return asdict(self)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- per-format parsers
def _parse_pdf(path: Path, ocr: OCREngine, doc: ParsedDocument) -> None:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    for pno, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            text = page.extract_text() or ""
        text = _tidy_layout(text)
        if len(text.strip()) >= _MIN_TEXT_CHARS:
            doc.pages.append(text)
            continue
        # No usable text layer: this page is a scan. OCR its largest embedded image.
        ocr_text = ""
        if ocr.available:
            try:
                images = sorted(page.images, key=lambda im: len(im.data), reverse=True)
            except Exception:
                images = []
            if images:
                suffix = Path(images[0].name).suffix or ".png"
                ocr_text = ocr.image_bytes_to_text(images[0].data, suffix)
        if ocr_text.strip():
            doc.pages.append(_tidy_layout(ocr_text))
            doc.ocr_pages.append(pno)
        else:
            doc.pages.append("")
            reason = "OCR produced no text" if ocr.available else "OCR is not installed (brew install tesseract)"
            doc.warnings.append(f"Page {pno} is scanned and could not be read: {reason}.")


def _parse_docx(path: Path, doc: ParsedDocument) -> None:
    # Read the XML directly: no extra runtime dependency, and no macros/embedded content executed.
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
    paragraphs = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.S):
        text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", para))
        if text.strip():
            paragraphs.append(_unescape_xml(text))
    doc.pages.append("\n".join(paragraphs))


def _parse_image(path: Path, ocr: OCREngine, doc: ParsedDocument) -> None:
    text = ocr.image_file_to_text(path) if ocr.available else ""
    if text.strip():
        doc.pages.append(_tidy_layout(text))
        doc.ocr_pages.append(1)
    else:
        doc.pages.append("")
        reason = "OCR produced no text" if ocr.available else "OCR is not installed (brew install tesseract)"
        doc.warnings.append(f"Image could not be read: {reason}.")


def _unescape_xml(s: str) -> str:
    return (
        s.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")
    )


def _tidy_layout(text: str) -> str:
    """Keep line structure (labels next to values) but drop trailing spaces and runs of blank lines."""
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    out: list[str] = []
    for ln in lines:
        if not ln.strip() and out and not out[-1].strip():
            continue
        out.append(ln)
    return "\n".join(out).strip()


# --------------------------------------------------------------------------- entry points
def parse_file(path: Path, rel: str, ocr: OCREngine, sha: str | None = None) -> ParsedDocument:
    suffix = path.suffix.lower()
    kind = {"pdf": "pdf", "docx": "docx"}.get(suffix.lstrip("."), "image" if suffix in IMAGE_SUFFIXES else "text")
    doc = ParsedDocument(file=rel, sha256=sha or sha256_file(path), kind=kind)
    try:
        if suffix == ".pdf":
            _parse_pdf(path, ocr, doc)
        elif suffix == ".docx":
            _parse_docx(path, doc)
        elif suffix in IMAGE_SUFFIXES:
            _parse_image(path, ocr, doc)
        else:
            doc.pages.append(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception as exc:  # a corrupt file must never take the whole index down
        doc.pages = doc.pages or [""]
        doc.warnings.append(f"Could not parse file: {type(exc).__name__}.")
    if not doc.text.strip() and not doc.warnings:
        doc.warnings.append("No readable text found.")
    return doc


class ParseCache:
    """Content-addressed cache of parse results (keyed by file hash + parser version + OCR state)."""

    def __init__(self, cache_dir: str | Path | None):
        self.dir = Path(cache_dir) if cache_dir else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self._mem: dict[str, ParsedDocument] = {}

    def _key(self, sha: str, ocr: OCREngine) -> str:
        return f"{sha}-v{PARSER_VERSION}-{'ocr' if ocr.available else 'noocr'}"

    def parse(self, path: Path, rel: str, ocr: OCREngine) -> ParsedDocument:
        sha = sha256_file(path)
        key = self._key(sha, ocr)
        cached = self._mem.get(key)
        if cached is None and self.dir and (self.dir / f"{key}.json").exists():
            try:
                cached = ParsedDocument(**json.loads((self.dir / f"{key}.json").read_text(encoding="utf-8")))
            except Exception:
                cached = None
        if cached is None:
            cached = parse_file(path, rel, ocr, sha=sha)
            if self.dir:
                (self.dir / f"{key}.json").write_text(json.dumps(cached.to_dict()), encoding="utf-8")
        self._mem[key] = cached
        # The same bytes may live under a different name; always report the current path.
        return ParsedDocument(**{**cached.to_dict(), "file": rel})

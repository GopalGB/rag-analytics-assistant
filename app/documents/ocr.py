"""Local OCR via the Tesseract command-line tool. Nothing leaves the machine.

Tesseract is optional: install it with `brew install tesseract` (macOS) or `apt install tesseract-ocr`.
When it is missing, scanned pages are NOT silently dropped — the parser records a warning so the
document is flagged for manual review instead of producing an empty or invented extraction.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class OCREngine:
    def __init__(self, enabled: bool = True, command: str = "tesseract", lang: str = "eng", timeout: int = 90):
        self.enabled = enabled
        self.command = command
        self.lang = lang
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self.enabled and shutil.which(self.command) is not None

    @property
    def name(self) -> str:
        if not self.enabled:
            return "disabled"
        return f"tesseract ({self.lang})" if self.available else "not installed"

    def image_bytes_to_text(self, data: bytes, suffix: str = ".png") -> str:
        """OCR one image. Returns "" on failure (callers flag the page as unreadable)."""
        if not self.available:
            return ""
        with tempfile.TemporaryDirectory() as tmp:
            img = Path(tmp) / f"page{suffix if suffix.startswith('.') else '.' + suffix}"
            img.write_bytes(data)
            try:
                proc = subprocess.run(
                    [self.command, str(img), "stdout", "-l", self.lang],
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            except (subprocess.TimeoutExpired, OSError):
                return ""
        return proc.stdout if proc.returncode == 0 else ""

    def image_file_to_text(self, path: Path) -> str:
        return self.image_bytes_to_text(path.read_bytes(), path.suffix.lower())

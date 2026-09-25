"""Dates that matter, read out of the documents: renewals, expiries, notice periods, due dates,
inspections and milestones, each with the sentence it came from and its file and page.

Deterministic (no AI model): a date only counts when a deadline word is in the same sentence, so
"Generated on 3 May" or a revision date is ignored. Numeric dates like 05/06/2026 are skipped
because their order is ambiguous; invoices are skipped because their due dates come from QuickBooks.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
     "december"], start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE = re.compile(
    rf"\b(?:(\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON}\.?,?\s+(\d{{4}})"  # 30 June 2029
    rf"|{_MON}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})"      # June 30, 2029
    r"|(\d{4})-(\d{2})-(\d{2}))\b",                                  # 2029-06-30
    re.I,
)
# (kind, pattern) — the first that matches the sentence names the deadline
_KINDS = [
    ("notice", r"\bnotice\b"),
    ("renewal", r"\brenew\w*"),
    ("expiry", r"\b(expir\w*|end date|ends?\b|terminat\w*)"),
    ("overdue", r"\boverdue\b"),
    ("inspection", r"\b(inspection|certificate|sign-?off|audit)\b"),
    ("payment", r"\b(pay\w*|invoice|instal?ment)\b"),
    ("completion", r"\b(complet\w*|handover|practical completion|milestone|deliver\w*)\b"),
    ("due", r"\b(due|deadline|by|no later than|before|until|review)\b"),
]
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


@dataclass
class Deadline:
    date: str
    kind: str
    text: str
    file: str
    page: int | None
    days: int  # from the "as of" date; negative = in the past
    status: str  # overdue | due_soon | upcoming | later

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse(m: re.Match) -> date | None:
    g = m.groups()
    try:
        if g[0]:
            return date(int(g[2]), _MONTHS[g[1].lower()[:3]], int(g[0]))
        if g[3]:
            return date(int(g[5]), _MONTHS[g[3].lower()[:3]], int(g[4]))
        return date(int(g[6]), int(g[7]), int(g[8]))
    except (ValueError, KeyError):
        return None


def _status(days: int, overdue_words: bool) -> str:
    if days < 0:
        return "overdue" if overdue_words or days >= -365 else "past"
    if days <= 30:
        return "due_soon"
    if days <= 180:
        return "upcoming"
    return "later"


def _sentences(text: str) -> list[str]:
    """Sentences, with lines re-joined inside a paragraph (PDF text wraps mid-sentence, even mid-date)."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        lines = [line.strip(" \t*•#") for line in para.split("\n") if line.strip(" \t*•#")]
        # a short first line with no punctuation is a heading ("Schedule"), not part of the sentence
        if len(lines) > 1 and len(lines[0].split()) <= 4 and not re.search(r"[.:;,!?]$", lines[0]):
            out.append(lines.pop(0))
        joined = " ".join(lines)
        out.extend(s.strip() for s in _SENTENCE_END.split(joined) if s.strip())
    return out


def find_deadlines(documents: list, as_of: date, skip_invoices: bool = True, limit_per_file: int = 12) -> list[Deadline]:
    """Scan parsed documents (with `.file` and `.pages` or `.text`) for dated obligations."""
    from app.invoices.registry import is_invoice_document

    found: dict[tuple[str, str, str], Deadline] = {}
    for doc in documents:
        if skip_invoices and is_invoice_document(doc):
            continue
        pages = getattr(doc, "pages", None) or [getattr(doc, "text", "")]
        per_file = 0
        for page_no, page_text in enumerate(pages, start=1):
            for sentence in _sentences(page_text):
                for m in _DATE.finditer(sentence):
                    when = _parse(m)
                    if when is None or abs(when.year - as_of.year) > 15:
                        continue
                    kind = next((k for k, rx in _KINDS if re.search(rx, sentence, re.I)), None)
                    if kind is None:
                        continue
                    days = (when - as_of).days
                    status = _status(days, "overdue" in sentence.lower())
                    if status == "past":
                        continue  # long-gone history ("signed on 1 March 2021"), not something to act on
                    key = (doc.file, when.isoformat(), kind)
                    if key in found or per_file >= limit_per_file:
                        continue
                    found[key] = Deadline(when.isoformat(), kind, sentence[:280], doc.file,
                                          page_no if len(pages) > 1 else None, days, status)
                    per_file += 1
    order = {"overdue": 0, "due_soon": 1, "upcoming": 2, "later": 3}
    return sorted(found.values(), key=lambda d: (order[d.status], d.date))

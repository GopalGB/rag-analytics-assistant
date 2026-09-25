"""Privacy router: decide per request whether data may go to a cloud model, and minimise what does.

Data classes
    documents   contracts, policies, project records, other text
    invoices    supplier invoices (files under an `invoices/` folder)
    accounting  QuickBooks data, extracted invoices, reconciliation (tables qbo_*, invoices, invoice_reconciliation)
    bank        bank statements and transactions
    personal    HR / payroll / personal records (configure with SENSITIVE_PATHS)

Policy (from settings)
    ALLOW_CLOUD_AI=false       → everything local (master switch)
    CLOUD_ALLOWED_DATA=documents → only these classes may be sent to a cloud model (default: documents)
    REDACT_PII=true            → emails, phone numbers and account-like numbers are masked in anything
                                 sent to a cloud model
High-risk identifiers (card numbers, SSNs, IBANs, labelled bank account numbers) force local processing.

Enforcement happens in three places so it can't be bypassed by the model:
1. routing — a sensitive request only gets local candidate models;
2. the ToolBox — when a cloud model is active, SQL over non-allowed tables is refused and document
   hits from non-allowed classes are withheld (and the rest redacted);
3. conversation memory — earlier local-only turns are withheld from cloud models.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

DATA_CLASSES = ("documents", "invoices", "accounting", "bank", "personal")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("iban", "high", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?\b")),
    ("ssn", "high", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    ("bank_account", "high", re.compile(
        r"\b(?:account|acct|a/c|routing|aba|sort\s*code|bsb|swift|bic)\s*(?:no\.?|number|#|:)\s*[:#]?\s*([A-Z0-9][A-Z0-9 \-]{5,24}\d)",
        re.I)),
    ("card", "high", re.compile(r"\b\d(?:[ -]?\d){12,18}\b")),
    ("email", "medium", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("phone", "medium", re.compile(r"(?<![\w/])(?:\+?\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)|\d{2,4})[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?![\w/])")),
]
_LABELS = {"iban": "[IBAN]", "ssn": "[SSN]", "bank_account": "[ACCOUNT]", "card": "[CARD]", "email": "[EMAIL]",
           "phone": "[PHONE]"}


@dataclass
class Finding:
    kind: str
    severity: str
    start: int
    end: int


def scan(text: str) -> list[Finding]:
    """Find personal / financial identifiers. Cards must pass the Luhn check; spans never overlap."""
    found: list[Finding] = []
    taken: list[tuple[int, int]] = []
    for kind, severity, rx in _PATTERNS:
        for m in rx.finditer(text or ""):
            s, e = m.span(1) if kind == "bank_account" else m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            chunk = m.group(0)
            if kind == "card":
                digits = re.sub(r"\D", "", chunk)
                if not (13 <= len(digits) <= 19 and _luhn(digits)):
                    continue
            if kind == "phone" and len(re.sub(r"\D", "", chunk)) < 9:
                continue
            if kind == "phone" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", chunk.strip()):
                continue  # ISO date, not a phone number
            found.append(Finding(kind, severity, s, e))
            taken.append((s, e))
    return sorted(found, key=lambda f: f.start)


def redact(text: str) -> tuple[str, int]:
    findings = scan(text)
    out = text
    for f in reversed(findings):
        out = out[: f.start] + _LABELS[f.kind] + out[f.end :]
    return out, len(findings)


def high_risk(text: str) -> list[str]:
    return sorted({f.kind for f in scan(text) if f.severity == "high"})


# --------------------------------------------------------------------------- classification
def parse_sensitive_paths(value: str | None) -> list[tuple[str, str]]:
    """SENSITIVE_PATHS='hr/=personal,payroll/=personal,bank/=bank' → [(prefix, class)]."""
    rules = []
    for part in (value or "").split(","):
        prefix, _, cls = part.partition("=")
        if prefix.strip() and cls.strip() in DATA_CLASSES:
            rules.append((prefix.strip().lower(), cls.strip()))
    return rules


@dataclass
class PrivacyPolicy:
    allow_cloud: bool = False
    cloud_allowed: frozenset[str] = frozenset({"documents"})
    redact_pii: bool = True
    path_rules: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_settings(cls, s: Any) -> PrivacyPolicy:
        allowed = frozenset(c.strip() for c in (s.cloud_allowed_data or "").split(",") if c.strip() in DATA_CLASSES)
        return cls(bool(s.allow_cloud_ai), allowed, bool(s.redact_pii), parse_sensitive_paths(s.sensitive_paths))

    def classify_file(self, path: str) -> str:
        p = (path or "").lower()
        for prefix, cls in self.path_rules:
            if p.startswith(prefix) or f"/{prefix}" in p:
                return cls
        parts = p.split("/")
        if any("invoice" in d for d in parts[:-1]):
            return "invoices"
        if any(w in p for w in ("bank", "statement")):
            return "bank"
        if any(w in p for w in ("payroll", "salary", "salaries", "/hr/", "personnel", "employee")):
            return "personal"
        return "documents"

    @staticmethod
    def classify_table(table: str) -> str:
        t = table.lower()
        if t.startswith("qbo_") or t in ("invoices", "invoice_lines", "invoice_reconciliation"):
            return "accounting"
        if "bank" in t or "transaction" in t or "statement" in t:
            return "bank"
        if any(w in t for w in ("payroll", "salary", "employee")):
            return "personal"
        return "documents"

    def cloud_ok_for(self, data_class: str) -> bool:
        return self.allow_cloud and data_class in self.cloud_allowed


@dataclass
class PrivacyDecision:
    local_only: bool
    reasons: list[str] = field(default_factory=list)
    data_classes: set[str] = field(default_factory=set)
    redact: bool = False
    high_risk: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"local_only": self.local_only, "reasons": self.reasons, "data_classes": sorted(self.data_classes),
                "redact": self.redact, "high_risk": self.high_risk}


class PrivacyRouter:
    def __init__(self, policy: PrivacyPolicy):
        self.policy = policy

    def decide(self, question: str, intent_classes: set[str], passages: list[Any] | None = None,
               has_local: bool = False) -> PrivacyDecision:
        pol = self.policy
        classes = set(intent_classes)
        reasons: list[str] = []
        if not pol.allow_cloud:
            return PrivacyDecision(True, ["cloud AI is not approved (ALLOW_CLOUD_AI=false)"], classes)
        risky = high_risk(question)
        if risky:
            reasons.append(f"question contains {', '.join(risky)} details")
        blocked = sorted(c for c in classes if c not in pol.cloud_allowed)
        if blocked:
            reasons.append(f"{', '.join(blocked)} data stays on this machine (CLOUD_ALLOWED_DATA)")
        # Passages: if the BEST evidence is sensitive, answer locally (when a local model exists).
        # Sensitive passages further down are simply withheld from a cloud model by the ToolBox guard.
        top = (passages or [])[:1]
        sens = sorted({pol.classify_file(p.file) for p in top} - set(pol.cloud_allowed))
        if sens and has_local:
            reasons.append(f"the most relevant passage is {', '.join(sens)} data")
            classes |= set(sens)
        return PrivacyDecision(bool(reasons), reasons, classes, redact=pol.redact_pii, high_risk=risky)

    def question_local_only(self, question: str) -> bool:
        return not self.policy.allow_cloud or bool(high_risk(question))


class PrivacyGuard:
    """Attached to a ToolBox. `cloud` is set per attempt to whether the active model is a cloud model."""

    def __init__(self, policy: PrivacyPolicy):
        self.policy = policy
        self.cloud = False
        self.withheld = 0
        self.redactions = 0

    def table_allowed(self, table: str) -> bool:
        return not self.cloud or self.policy.cloud_ok_for(self.policy.classify_table(table))

    def file_allowed(self, path: str) -> bool:
        return not self.cloud or self.policy.cloud_ok_for(self.policy.classify_file(path))

    def outgoing(self, text: str) -> str:
        if not (self.cloud and self.policy.redact_pii):
            return text
        out, n = redact(text)
        self.redactions += n
        return out

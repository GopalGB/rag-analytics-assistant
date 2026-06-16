"""Versioned catalogue of guard patterns.

Patterns target malicious INTENT, not bare nouns, to keep false positives low — e.g. exfiltration
requires an action verb co-occurring with a secret noun, and unsafe-SQL requires real DDL/DML syntax
(`drop table`) rather than the bare word "create". The real SQL safety boundary is the DataStore's
read-only allowlist; this firewall is the first, cheap line of defence.
"""

from __future__ import annotations

import re

PATTERN_VERSION = "1.1.0"

# Instruction-override / system-prompt extraction / persona-jailbreak.
INJECTION = [
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+(instructions|prompts?|rules?)\b", re.I),
    re.compile(r"\bdisregard\s+(your|the|all)\s+(instructions|rules|guidelines)\b", re.I),
    re.compile(
        r"\b(reveal|show|print|repeat|leak)\s+(your|the)\s+(system\s+prompt|instructions|hidden\s+prompt)\b",
        re.I,
    ),
    re.compile(r"\byou\s+are\s+now\s+(a|an|in)\b.*\bmode\b", re.I),
    re.compile(r"\b(developer|admin|god|jailbreak|dan)\s+mode\b", re.I),
    re.compile(r"\bpretend\s+(you\s+are|to\s+be)\b", re.I),
    re.compile(r"\bact\s+as\s+(if\s+you\s+are\s+)?(an?\s+)?(unrestricted|uncensored|different)\b", re.I),
]

# Exfiltration: an action verb that, near a secret noun, signals an attempt to extract config/creds.
_SECRET_NOUN = r"(api[_\s-]?keys?|secrets?|passwords?|tokens?|credentials?|env(?:ironment)?\s+variables?)"
EXFILTRATION = [
    re.compile(
        rf"\b(reveal|show|print|leak|give\s+me|tell\s+me|list|what(?:'s| is| are)?\s+your)\b.{{0,40}}\b{_SECRET_NOUN}\b",
        re.I,
    ),
    re.compile(r"\b(\.env\b|os\.environ|getenv|connection\s+string|aws_secret)\b", re.I),
]

# Requests for general-purpose code/content generation outside the analytics scope.
CODE_REQUEST = [
    re.compile(r"\bwrite\s+(me\s+)?(a\s+)?(python|javascript|bash|sql|code|script|program|function)\b", re.I),
    re.compile(r"\b(write|compose)\s+(me\s+)?(a\s+)?(poem|essay|story|song|email|letter)\b", re.I),
    re.compile(r"\btranslate\s+(this|the\s+following)\b", re.I),
]

# Unsafe SQL smuggled in directly — match real DDL/DML syntax, not bare verbs.
UNSAFE_SQL = [
    re.compile(r"\bdrop\s+(table|view|schema|database)\b", re.I),
    re.compile(r"\btruncate\s+table\b", re.I),
    re.compile(r"\bdelete\s+from\b", re.I),
    re.compile(r"\binsert\s+into\b", re.I),
    re.compile(r"\bupdate\s+\w+\s+set\b", re.I),
    re.compile(r"\b(alter|create)\s+(table|view|schema|or\s+replace)\b", re.I),
    re.compile(r"\battach\b|\bcopy\s+\w+\s+to\b", re.I),
    re.compile(r"\bread_(csv|parquet|json|ndjson|text|blob)\s*\(", re.I),
    re.compile(r";\s*\w", re.I),  # stacked statements
]

# Output-format hijacking.
FORMAT_HIJACK = [
    re.compile(r"\bfrom\s+now\s+on\b.*\b(respond|reply|answer|prefix|format)\b", re.I),
    re.compile(r"\brespond\s+only\s+(with|in)\b", re.I),
]

CATALOG: dict[str, list[re.Pattern[str]]] = {
    "injection": INJECTION,
    "exfiltration": EXFILTRATION,
    "code_request": CODE_REQUEST,
    "unsafe_sql": UNSAFE_SQL,
    "format_hijack": FORMAT_HIJACK,
}

GREETINGS = re.compile(
    r"^\s*(hi|hii|hey|hello|yo|good\s+(morning|afternoon|evening)|thanks?|thank\s+you)\b[\s!.?]*$", re.I
)


def first_match(text: str) -> str | None:
    """Return the category of the first pattern that matches, or None."""
    for category, patterns in CATALOG.items():
        for pat in patterns:
            if pat.search(text):
                return category
    return None

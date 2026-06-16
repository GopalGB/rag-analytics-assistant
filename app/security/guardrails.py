"""Input firewall. Decides allow / refuse / greeting BEFORE any retrieval or model call."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.security import patterns

_REFUSALS = {
    "injection": "I can only help with analytics over the loaded data. I can't change my role or instructions.",
    "exfiltration": "I can't share configuration, credentials, or internal instructions. Ask me about the data instead.",
    "code_request": "I answer data questions — I don't write general code or free-form content.",
    "unsafe_sql": "I only run safe, read-only lookups. Tell me what you'd like to know and I'll query it.",
    "format_hijack": "I'll keep my normal answer format. What would you like to know about the data?",
    "too_long": "That question is too long. Please shorten it.",
    "out_of_scope": "That looks outside the loaded data. Ask me about the available tables or documents.",
}

GREETING_REPLY = (
    "Hi! Ask me a question about the loaded data or documents and I'll dig in."
)


@dataclass
class Decision:
    allowed: bool
    category: str  # "ok" | "greeting" | <refusal category>
    user_message: str
    input_hash: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class InputGuard:
    """Stateless guard. `scope_terms` optionally restricts questions to the loaded domain."""

    def __init__(
        self, max_input_chars: int = 2000, scope_terms: set[str] | None = None
    ):
        self.max_input_chars = max_input_chars
        self.scope_terms = scope_terms or set()

    def inspect(self, text: str) -> Decision:
        text = (text or "").strip()
        h = _hash(text)

        if not text:
            return Decision(False, "out_of_scope", _REFUSALS["out_of_scope"], h)
        if len(text) > self.max_input_chars:
            return Decision(False, "too_long", _REFUSALS["too_long"], h)
        if patterns.GREETINGS.match(text):
            return Decision(True, "greeting", GREETING_REPLY, h)

        category = patterns.first_match(text)
        if category:
            return Decision(False, category, _REFUSALS[category], h)

        return Decision(True, "ok", "", h)

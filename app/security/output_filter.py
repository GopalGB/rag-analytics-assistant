"""Post-generation scrubber. Last line of defence against leaked prompt/secret/code output."""

from __future__ import annotations

import re

_REDACTION = "[redacted]"

_LEAK_PATTERNS = [
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{16,}"),  # OpenAI / Anthropic style keys
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key ids
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),  # GitHub tokens
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),  # Groq keys
    re.compile(r"xai-[A-Za-z0-9]{20,}"),  # xAI keys
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"AIza[0-9A-Za-z_-]{30,}"),  # Google API keys
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWTs
    re.compile(r"SECURITY DIRECTIVE.*", re.S),  # leaked armor block
    re.compile(r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*\S+"),
]

# The assistant should never emit code; redact ALL fenced blocks (this is defence-in-depth, not the
# primary control). It is intentionally aggressive — a SQL readout belongs in the structured payload.
_CODE_FENCE = re.compile(r"```[a-zA-Z]*\n.*?```", re.S)


_SECRETS: set[str] = set()


def register_secret(value: str | None) -> None:
    """Redact this exact value (e.g. a configured provider API key) from every answer."""
    if value and len(value) >= 8:
        _SECRETS.add(value)


def scrub(text: str) -> str:
    """Redact credential-shaped strings and a leaked system prompt; drop all fenced code blocks."""
    if not text:
        return text
    out = text
    for secret in _SECRETS:
        out = out.replace(secret, _REDACTION)
    for pat in _LEAK_PATTERNS:
        out = pat.sub(_REDACTION, out)
    out = _CODE_FENCE.sub(_REDACTION, out)
    return out.strip()

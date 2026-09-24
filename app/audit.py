"""Append-only, tamper-evident activity log (JSON Lines, hash-chained).

Every entry stores the SHA-256 of the previous entry, so editing or deleting a past line breaks the
chain and `verify()` reports where. The log stays on the local machine.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


def _request_id() -> str:
    from app.observability import request_id_var

    return request_id_var.get()


def _digest(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._memory: list[dict[str, Any]] = []
        self._last = GENESIS
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                tail = self.tail(1)
                if tail:
                    self._last = tail[-1]["hash"]

    def record(self, event: str, actor: str = "local-user", **details: Any) -> dict[str, Any]:
        with self._lock:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event,
                "actor": actor,
                "details": details,
                "prev": self._last,
            }
            rid = _request_id()
            if rid != "-":
                entry["request_id"] = rid
            entry["hash"] = _digest(entry)
            if self.path:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, default=str) + "\n")
            else:
                self._memory.append(entry)
            self._last = entry["hash"]
        return entry

    def _entries(self) -> list[dict[str, Any]]:
        if not self.path:
            return list(self._memory)
        if not self.path.exists():
            return []
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        out.append({"_unreadable": True})
        return out

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.path:
            return self._memory[-limit:]
        if not self.path.exists():
            return []
        buf: deque[str] = deque(maxlen=max(1, limit))
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    buf.append(line)
        out = []
        for line in buf:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # verify() reports it; reading the log must not crash the app
        return out

    def verify(self) -> dict[str, Any]:
        prev = GENESIS
        entries = self._entries()
        for i, e in enumerate(entries):
            if e.get("_unreadable") or e.get("prev") != prev or _digest(e) != e.get("hash"):
                return {"ok": False, "entries": len(entries), "broken_at": i + 1}
            prev = e["hash"]
        return {"ok": True, "entries": len(entries)}

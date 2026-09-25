"""Answer cache for the stateless public demo.

The hosted demo has no conversation memory and read-only data, so the same question always deserves
the same answer. Reusing it makes repeated questions instant and keeps a free-tier model quota for new
ones. Only real model answers are kept (never refusals, "not found" fallbacks or degraded answers), and
every reuse is labelled `cached: true` so the interface can say so.

An optional seed file holds answers generated ahead of time (scripts/build_answer_cache.py). It is used
only when its corpus fingerprint matches the documents being served, so a changed document can never be
answered from a stale seed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_NOISE = re.compile(r"[\s?.!]+")


def normalize(question: str) -> str:
    return _NOISE.sub(" ", question.lower()).strip()


def corpus_fingerprint(data_dir: str | Path) -> str:
    """Content hash of every file under the data directory (names + bytes)."""
    root = Path(data_dir)
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def cacheable(payload: dict[str, Any]) -> bool:
    return payload.get("route") == "agent" and bool((payload.get("routing") or {}).get("model"))


class AnswerCache:
    def __init__(self, max_entries: int = 256):
        self.max_entries = max(1, max_entries)
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_seed(
        cls, seed_path: str | Path | None, data_dir: str | Path, max_entries: int = 256
    ) -> AnswerCache:
        cache = cls(max_entries)
        path = Path(seed_path) if seed_path else None
        if not path or not path.is_file():
            return cache
        try:
            seed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cache
        if seed.get("fingerprint") != corpus_fingerprint(data_dir):
            return cache  # answers were generated from different documents
        for question, payload in (seed.get("answers") or {}).items():
            if isinstance(payload, dict):
                cache.put(question, payload)
        return cache

    def __len__(self) -> int:
        return len(self._items)

    def get(self, question: str) -> dict[str, Any] | None:
        key = normalize(question)
        with self._lock:
            payload = self._items.get(key)
            if payload is None:
                return None
            self._items.move_to_end(key)
            hit = copy.deepcopy(payload)
        hit["cached"] = True
        return hit

    def put(self, question: str, payload: dict[str, Any]) -> None:
        if not cacheable(payload):
            return
        stored = copy.deepcopy(payload)
        stored.pop("cached", None)
        stored.setdefault("cached_at", datetime.now(UTC).isoformat(timespec="seconds"))
        with self._lock:
            self._items[normalize(question)] = stored
            self._items.move_to_end(normalize(question))
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)

    def export(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(dict(self._items))

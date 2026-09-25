"""Streaming answers as server-sent events.

`stream_answer()` runs the normal `engine.answer()` in a worker thread and yields events as they
happen, so the UI can show progress and the answer as it is written:

    {"type": "status", "stage": "routing"}
    {"type": "route", "intent": "documents", "tier": "fast", "tools": [...], "local_only": false, ...}
    {"type": "model", "model": "anthropic:claude-haiku-4-5-20251001", "local": false}
    {"type": "tool", "tool": "search_docs", "state": "start" | "done", "ok": true}
    {"type": "text", "text": "<answer so far>"}      ← a scrubbed SNAPSHOT, not a raw delta
    {"type": "reset"}                                  ← discard text (tool turn, or fallback to next model)
    {"type": "done", "payload": {...}}                 ← identical to the POST /chat response
    {"type": "error", "message": "..."}

Safety: text is never forwarded raw. Every snapshot is passed through the output scrubber, and the
last characters are held back until more text arrives, so a credential or leaked-prompt pattern is
redacted before any part of it reaches the browser. The final `done` payload is authoritative.
"""

from __future__ import annotations

import contextvars
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

from app.security import scrub
from app.security.output_filter import holdback_chars

_END = object()


class _Snapshotter:
    def __init__(self, out: queue.Queue, min_interval: float = 0.05):
        self.out = out
        self.parts: list[str] = []
        self.sent = ""
        self.min_interval = min_interval
        self.last = 0.0

    def add(self, delta: str) -> None:
        self.parts.append(delta)
        now = time.monotonic()
        if now - self.last >= self.min_interval:
            self.flush(final=False)
            self.last = now

    def flush(self, final: bool) -> None:
        safe = scrub("".join(self.parts))
        if not final:
            hold = holdback_chars()
            safe = safe[:-hold] if len(safe) > hold else ""
        if safe and safe != self.sent:
            self.sent = safe
            self.out.put({"type": "text", "text": safe})

    def reset(self) -> None:
        self.parts.clear()
        self.sent = ""
        self.out.put({"type": "reset"})


def stream_answer(engine: Any, session_id: str, question: str, actor: str = "local-user",
                  timeout: float = 600.0) -> Iterator[dict[str, Any]]:
    out: queue.Queue = queue.Queue()
    snap = _Snapshotter(out)

    def sink(kind: str, data: Any = None) -> None:
        if kind == "text":
            snap.add(data or "")
        elif kind == "reset":
            snap.reset()
        else:
            out.put({"type": kind, **(data or {})})

    def run() -> None:
        try:
            payload = engine.answer(session_id, question, actor=actor, sink=sink)
            out.put({"type": "done", "payload": payload})
        except Exception as exc:  # never leak internals
            out.put({"type": "error", "message": f"The assistant failed ({type(exc).__name__})."})
        finally:
            out.put(_END)

    ctx = contextvars.copy_context()  # keep the request ID for logs and the activity log
    threading.Thread(target=lambda: ctx.run(run), name="answer-stream", daemon=True).start()
    deadline = time.monotonic() + timeout
    while True:
        try:
            ev = out.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            yield {"type": "error", "message": "The answer timed out."}
            return
        if ev is _END:
            return
        yield ev

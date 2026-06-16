"""Per-session conversation memory so the assistant can handle follow-up questions."""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock


class ConversationMemory:
    def __init__(self, max_turns: int = 8):
        self.max_messages = max_turns * 2  # one user + one assistant per turn
        self._store: dict[str, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=self.max_messages)
        )
        self._lock = Lock()

    def history(self, session_id: str) -> list[dict[str, str]]:
        with self._lock:
            return list(self._store[session_id])

    def add(self, session_id: str, role: str, content: str) -> None:
        with self._lock:
            self._store[session_id].append({"role": role, "content": content})

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

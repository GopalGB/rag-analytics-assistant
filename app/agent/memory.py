"""Per-session conversation memory so the assistant can handle follow-up questions.

Turns that involved local-only data are marked, and are withheld when the next turn is answered by a
cloud model — so a follow-up question can't leak an earlier accounting answer off the machine.
"""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock

WITHHELD = "[earlier message withheld: it involved data that must stay on this machine]"


class ConversationMemory:
    def __init__(self, max_turns: int = 8):
        self.max_messages = max_turns * 2  # one user + one assistant per turn
        self._store: dict[str, deque[dict]] = defaultdict(lambda: deque(maxlen=self.max_messages))
        self._lock = Lock()

    def history(self, session_id: str, for_cloud: bool = False) -> list[dict[str, str]]:
        with self._lock:
            items = list(self._store[session_id])
        return [
            {"role": m["role"], "content": WITHHELD if (for_cloud and m.get("local_only")) else m["content"]}
            for m in items
        ]

    def add(self, session_id: str, role: str, content: str, local_only: bool = False) -> None:
        with self._lock:
            self._store[session_id].append({"role": role, "content": content, "local_only": local_only})

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

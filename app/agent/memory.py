"""Per-session conversation memory so the assistant can handle follow-up questions.

Turns that involved local-only data are marked, and are withheld when the next turn is answered by a
cloud model — so a follow-up question can't leak an earlier accounting answer off the machine.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from threading import Lock

WITHHELD = "[earlier message withheld: it involved data that must stay on this machine]"


class ConversationMemory:
    """At most `max_sessions` conversations are kept (least recently used dropped first); `max_turns=0`
    turns memory off entirely (used by the public demo)."""

    def __init__(self, max_turns: int = 8, max_sessions: int = 1000):
        self.max_messages = max_turns * 2  # one user + one assistant per turn
        self.max_sessions = max_sessions
        self._store: OrderedDict[str, deque[dict]] = OrderedDict()
        self._lock = Lock()

    def session_count(self) -> int:
        with self._lock:
            return len(self._store)

    def history(self, session_id: str, for_cloud: bool = False) -> list[dict[str, str]]:
        with self._lock:
            items = list(self._store.get(session_id, ()))
        return [
            {"role": m["role"], "content": WITHHELD if (for_cloud and m.get("local_only")) else m["content"]}
            for m in items
        ]

    def add(self, session_id: str, role: str, content: str, local_only: bool = False) -> None:
        if self.max_messages <= 0:
            return
        with self._lock:
            turns = self._store.get(session_id)
            if turns is None:
                turns = self._store[session_id] = deque(maxlen=self.max_messages)
                while len(self._store) > self.max_sessions:
                    self._store.popitem(last=False)
            self._store.move_to_end(session_id)
            turns.append({"role": role, "content": content, "local_only": local_only})

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._store.pop(session_id, None)

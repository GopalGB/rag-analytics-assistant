"""Human-approval gate for anything that would act outside the assistant.

The assistant (or a user) can only PROPOSE an action — a draft email, recording a bill in QuickBooks,
a follow-up task. A named person must approve or reject it. In this prototype, approved actions are
recorded but NOT executed: external writes are disabled by design, so the approval trail can be
demonstrated safely before any live connection is allowed to change anything.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ACTION_TYPES = {
    "draft_email": "Send an email",
    "record_bill": "Record a bill in QuickBooks",
    "follow_up_task": "Create a follow-up task",
    "other": "Other external action",
}
EXECUTION_NOTE = (
    "Approved and logged. Not executed: this prototype is read-only, so external actions are disabled. "
    "In a later phase, approved actions would be carried out here."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApprovalQueue:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._items: list[dict[str, Any]] = []
        if self.path and self.path.exists():
            try:
                self._items = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._items = []

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(i) for i in self._items]
        items = [i for i in items if status is None or i["status"] == status]
        return sorted(items, key=lambda i: i["created_at"], reverse=True)

    def propose(
        self, action_type: str, title: str, details: str, proposed_by: str, dedupe_key: str | None = None
    ) -> dict[str, Any]:
        if action_type not in ACTION_TYPES:
            action_type = "other"
        with self._lock:
            if dedupe_key:
                for item in self._items:
                    if item.get("dedupe_key") == dedupe_key and item["status"] == "pending":
                        return dict(item)
            item = {
                "id": secrets.token_hex(4),
                "action_type": action_type,
                "action_label": ACTION_TYPES[action_type],
                "title": (title or "").strip()[:200] or ACTION_TYPES[action_type],
                "details": (details or "").strip()[:4000],
                "proposed_by": proposed_by,
                "created_at": _now(),
                "status": "pending",
                "decided_by": None,
                "decided_at": None,
                "decision_note": None,
                "execution": None,
                "dedupe_key": dedupe_key,
            }
            self._items.append(item)
            self._save()
            return dict(item)

    def decide(self, item_id: str, decision: str, reviewer: str, note: str = "") -> dict[str, Any]:
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be 'approved' or 'rejected'")
        if not reviewer.strip():
            raise ValueError("a reviewer name is required")
        with self._lock:
            item = next((i for i in self._items if i["id"] == item_id), None)
            if item is None:
                raise KeyError(item_id)
            if item["status"] != "pending":
                raise ValueError(f"already {item['status']}")
            item.update(
                status=decision,
                decided_by=reviewer.strip()[:80],
                decided_at=_now(),
                decision_note=(note or "").strip()[:500],
                execution=EXECUTION_NOTE if decision == "approved" else "Rejected; nothing was done.",
            )
            self._save()
            return dict(item)

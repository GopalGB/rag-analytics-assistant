"""Activity log tamper-evidence and the human-approval gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.approvals import ApprovalQueue
from app.audit import AuditLog


def test_audit_chain_detects_tampering(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.record("event", actor="tester", n=i)
    assert log.verify() == {"ok": True, "entries": 3}
    # a restart continues the same chain
    AuditLog(path).record("event", n=3)
    assert AuditLog(path).verify()["ok"]
    lines = path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["details"]["n"] = 99  # edit history
    lines[1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n")
    result = AuditLog(path).verify()
    assert result["ok"] is False and result["broken_at"] == 2


def test_audit_detects_deleted_entry(tmp_path: Path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.record("event", n=i)
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert AuditLog(path).verify()["ok"] is False


def test_approval_lifecycle(tmp_path: Path):
    q = ApprovalQueue(tmp_path / "approvals.json")
    a = q.propose("draft_email", "Chase Oakridge", "Dear...", proposed_by="assistant", dedupe_key="k1")
    assert q.propose("draft_email", "dup", "", proposed_by="x", dedupe_key="k1")["id"] == a["id"]
    assert q.propose("rm -rf", "weird", "", proposed_by="x")["action_type"] == "other"
    with pytest.raises(ValueError):
        q.decide(a["id"], "approved", reviewer="")
    with pytest.raises(ValueError):
        q.decide(a["id"], "maybe", reviewer="Owner")
    done = q.decide(a["id"], "approved", reviewer="Owner", note="ok")
    assert done["status"] == "approved" and "Not executed" in done["execution"]
    with pytest.raises(ValueError):
        q.decide(a["id"], "rejected", reviewer="Owner")
    with pytest.raises(KeyError):
        q.decide("nope", "approved", reviewer="Owner")
    # persisted
    assert ApprovalQueue(tmp_path / "approvals.json").list("approved")[0]["decided_by"] == "Owner"

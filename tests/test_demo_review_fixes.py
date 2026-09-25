"""Regression tests for the review of the demo-resilience commits: Retry-After sanitising, the router's
wait guard, the SQL shown with an answer, and what the answer-cache seed fingerprint covers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

from app.agent.answer_cache import AnswerCache, corpus_fingerprint, seed_fingerprint
from app.agent.tools import ToolBox
from app.config import Settings
from app.llm.providers import LLMError, retry_after_seconds
from app.llm.router import ModelRouter, NoModelAvailable

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- Retry-After
def test_retry_after_accepts_an_http_date():
    when = datetime.now(timezone.utc) + timedelta(seconds=30)
    secs = retry_after_seconds({"retry-after": format_datetime(when, usegmt=True)}, "")
    assert secs is not None and 25 <= secs <= 31


@pytest.mark.parametrize("value", ["-5", "nan", "inf", "-inf", "Wed, 21 Oct 2015 07:28:00 GMT", "soon"])
def test_retry_after_rejects_unusable_header_values(value):
    assert retry_after_seconds({"retry-after": value}, "") is None


def test_retry_after_rejects_an_overflowing_body_hint():
    assert retry_after_seconds({}, "Please try again in " + "9" * 400 + "s.") is None


# --------------------------------------------------------------------------- router wait guard
class _Limited:
    def __init__(self, retry_after):
        self.name, self.is_local, self.provider, self.model = "a", False, "groq", "a"
        self.retry_after, self.calls = retry_after, 0

    def __call__(self):
        self.calls += 1
        if self.calls == 1:
            raise LLMError("rate limited", status=429, retry_after=self.retry_after)
        return "ok"


def _router(wait, hint):
    router = ModelRouter({"fast": [_Limited(hint)]}, rate_limit_max_wait=wait)
    router.slept = []
    router.sleep = router.slept.append
    return router


@pytest.mark.parametrize("cap,hint", [(0, 0.0), (-1, 0.0), (10, -3.0)])
def test_router_never_retries_when_waiting_is_off_or_the_hint_is_negative(cap, hint):
    router = _router(cap, hint)
    with pytest.raises(NoModelAvailable):
        router.run("fast", lambda m: m())
    assert router.slept == []


def test_zero_hint_with_a_positive_cap_retries_immediately():
    router = _router(10, 0.0)
    assert router.run("fast", lambda m: m())[0] == "ok" and router.slept == [0.0]


# --------------------------------------------------------------------------- SQL shown with an answer
def test_a_rejected_query_does_not_replace_the_sql_of_the_rows_shown(store, retriever):
    tb = ToolBox(store, retriever)
    tb.run("run_sql", {"sql": "SELECT region FROM sales"})
    tb.run("run_sql", {"sql": "customer_name"})  # the model sent a fragment: rejected
    tb.run("run_sql", {"sql": "SELECT nope FROM missing_table"})  # fails
    assert tb.last_sql == "SELECT region FROM sales" and tb.columns == ["region"]


# --------------------------------------------------------------------------- seed fingerprint
def _settings(tmp_path, fixture, as_of):
    return Settings(data_dir=str(tmp_path / "docs"), qbo_fixture=str(fixture), report_as_of=as_of)


def test_seed_fingerprint_covers_the_quickbooks_fixture_and_report_date(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("fee is 5")
    fixture = tmp_path / "qbo.json"
    fixture.write_text('{"bills": []}')
    base = seed_fingerprint(_settings(tmp_path, fixture, "2026-07-15"))
    assert seed_fingerprint(_settings(tmp_path, fixture, "2026-08-01")) != base  # "days overdue" changes
    fixture.write_text('{"bills": [1]}')
    assert seed_fingerprint(_settings(tmp_path, fixture, "2026-07-15")) != base  # QuickBooks figures change
    assert corpus_fingerprint(tmp_path / "docs") != base  # documents alone are not enough

    seed = tmp_path / "seed.json"
    answer = {"text": "5 [a.md]", "route": "agent", "sources": [{"file": "a.md"}], "routing": {"model": "groq:m"}}
    seed.write_text(json.dumps({"fingerprint": base, "answers": {"what is the fee": answer}}))
    assert AnswerCache.from_seed(seed, tmp_path / "docs", fingerprint=base).get("What is the fee?")
    assert AnswerCache.from_seed(seed, tmp_path / "docs", fingerprint="other").get("What is the fee?") is None


def test_shipped_seed_matches_the_documented_demo_settings():
    env = dict(line.split("=", 1) for line in (ROOT / "deploy" / "vercel.env.example").read_text().splitlines()
               if "=" in line and not line.lstrip().startswith("#"))
    settings = Settings(data_dir=str(ROOT / "data" / "sample"), report_as_of=env.get("REPORT_AS_OF") or None)
    seed = json.loads((ROOT / "data" / "demo_answer_cache.json").read_text())
    assert seed["fingerprint"] == seed_fingerprint(settings)  # otherwise the hosted demo ignores the seed
    for payload in seed["answers"].values():
        if payload.get("sql"):
            assert payload["sql"].lstrip().lower().startswith(("select", "with"))

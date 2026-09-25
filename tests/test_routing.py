"""Model router, task router, privacy router and type-safe structured outputs — and the engine using them."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.agent.engine import AgentEngine
from app.agent.memory import WITHHELD, ConversationMemory
from app.agent.tools import ToolBox
from app.llm import usage
from app.llm.intent import IntentRouter
from app.llm.privacy import PrivacyGuard, PrivacyPolicy, PrivacyRouter, high_risk, redact
from app.llm.router import ModelRouter, NoModelAvailable, parse_pricing
from app.llm.schemas import InvoiceFields, RouteDecision, tool_spec
from app.llm.structured import StructuredOutputError, extract_json, generate
from app.security import InputGuard


class Model:
    """Scriptable fake model: `reply` may be a string, an exception, or a callable(system, question, toolbox)."""

    def __init__(self, name, local, reply="ok", json_replies=None, tokens=(0, 0)):
        self.name, self.is_local, self.provider, self.model = name, local, name.split(":")[0], name.split(":")[-1]
        self.reply, self.json_replies = reply, list(json_replies or [])
        self.tokens = tokens
        self.seen: list[dict] = []

    def converse(self, system, history, question, toolbox, max_iters):
        self.seen.append({"system": system, "history": history, "question": question, "tools": [t["function"]["name"] for t in toolbox.tool_specs()]})
        usage.add(*self.tokens)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply(system, question, toolbox) if callable(self.reply) else self.reply

    def complete(self, system, prompt):
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.json_replies.pop(0) if self.json_replies else "{}"


# --------------------------------------------------------------------------- model router
def test_fallback_order_metrics_and_cost():
    bad = Model("anthropic:claude-sonnet-5", False, TimeoutError("slow"))
    good = Model("openai:gpt-4o", False, "fine", tokens=(1000, 200))
    router = ModelRouter({"strong": [bad, good]}, pricing=parse_pricing('{"openai:gpt-4o": [2.5, 10]}'))
    out, trace = router.run("strong", _raise_or)
    assert out == "fine"
    t = trace.to_dict()
    assert t["model"] == "openai:gpt-4o" and t["fallbacks"] == 1
    assert t["attempts"][0]["error"].startswith("TimeoutError")
    stats = {m["model"]: m for m in router.describe()["models"]}
    assert stats["anthropic:claude-sonnet-5"]["failures"] == 1


def _raise_or(m):
    usage.add(*m.tokens)
    if isinstance(m.reply, Exception):
        raise m.reply
    return m.reply


def test_cost_and_tokens_recorded():
    good = Model("openai:gpt-4o", False, "fine", tokens=(1_000_000, 100_000))
    router = ModelRouter({"fast": [good]}, pricing=parse_pricing('{"openai:gpt-4o": [2.5, 10]}'))
    _, trace = router.run("fast", _raise_or)
    rec = trace.attempts[0]
    assert (rec.input_tokens, rec.output_tokens) == (1_000_000, 100_000)
    assert rec.cost_usd == pytest.approx(3.5)


def test_local_only_filter_and_tier_fallback():
    cloud, local = Model("openai:gpt-4o", False), Model("ollama:qwen2.5:14b", True)
    router = ModelRouter({"strong": [cloud], "fast": [local]})
    assert [m.name for m in router.candidates("strong")] == ["openai:gpt-4o", "ollama:qwen2.5:14b"]
    assert [m.name for m in router.candidates("strong", local_only=True)] == ["ollama:qwen2.5:14b"]
    assert ModelRouter({"strong": [cloud]}).candidates("fast", local_only=True) == []
    with pytest.raises(NoModelAvailable):
        ModelRouter({"strong": [cloud]}).run("strong", _raise_or, local_only=True)


def test_circuit_breaker_skips_failing_model():
    flaky = Model("groq:x", False, RuntimeError("down"))
    ok = Model("openai:y", False, "ok")
    router = ModelRouter({"fast": [flaky, ok]}, failure_threshold=2, cooldown_seconds=60)
    for _ in range(2):
        router.run("fast", _raise_or)
    assert [m.name for m in router.candidates("fast")] == ["openai:y"]  # flaky is paused
    assert {m["model"]: m for m in router.describe()["models"]}["groq:x"]["circuit_open"]


def test_all_models_fail():
    router = ModelRouter({"fast": [Model("a:1", True, RuntimeError("x")), Model("b:2", True, RuntimeError("y"))]})
    with pytest.raises(NoModelAvailable) as e:
        router.run("fast", _raise_or)
    assert len(e.value.attempts) == 2


# --------------------------------------------------------------------------- task router
@pytest.mark.parametrize(
    "question,intent",
    [
        ("When does the office lease expire and what notice is needed to renew?", "documents"),
        ("What is the liability cap in the supply agreement?", "documents"),
        ("Which customers have overdue balances?", "accounting"),
        ("Which supplier invoices don't match QuickBooks?", "accounting"),
        ("How much did we spend with each vendor?", "accounting"),
        ("Draft an email to Oakridge Accounting Partners about their overdue invoices", "drafting"),
        ("Write a short summary report of the Riverside project", "drafting"),
    ],
)
def test_rules_route_common_requests(question, intent):
    plan = IntentRouter(model_fallback=False).plan(question)
    assert plan.intent == intent, plan.scores


def test_pipelines_restrict_tools_and_tiers():
    router = IntentRouter(model_fallback=False)
    docs = router.plan("What does the lease say about renewal notice?")
    assert docs.tools == ("search_docs",) and docs.tier == "fast"
    acct = router.plan("Which customers have overdue balances?")
    assert "run_sql" in acct.tools and acct.tier == "strong" and "accounting" in acct.data_classes
    assert "propose_action" in router.plan("Draft an email reminder to Juniper").tools


def test_model_classifies_when_rules_unsure():
    calls = []

    def classify(q):
        calls.append(q)
        return RouteDecision(intent="accounting", confidence=0.7, reason="asks for figures")

    plan = IntentRouter().plan("hmm, how are things looking lately?", classify=classify)
    assert calls and plan.intent == "accounting" and plan.method == "model"
    # a failing classifier falls back to the safe default
    assert IntentRouter().plan("hmm?", classify=lambda q: (_ for _ in ()).throw(RuntimeError())).intent == "general"


# --------------------------------------------------------------------------- privacy router
def test_pii_detection_and_redaction():
    assert high_risk("pay card 4111 1111 1111 1111") == ["card"]
    assert high_risk("4111 1111 1111 1112") == []  # fails Luhn
    assert high_risk("SSN 123-45-6789") == ["ssn"]
    assert high_risk("IBAN GB29 NWBK 6016 1331 9268 19") == ["iban"]
    assert high_risk("Account number: 12345678") == ["bank_account"]
    text, n = redact("Email dana@example.com or call +1 (555) 123-4567 about INV-10421 on 2026-05-04")
    assert n == 2 and "[EMAIL]" in text and "[PHONE]" in text and "INV-10421" in text and "2026-05-04" in text


def _policy(**kw):
    return PrivacyPolicy(**{"allow_cloud": True, "cloud_allowed": frozenset({"documents"}), "redact_pii": True, **kw})


def test_privacy_decisions():
    pr = PrivacyRouter(_policy())
    assert not pr.decide("What does the lease say?", {"documents"}).local_only
    d = pr.decide("Which customers are overdue?", {"accounting", "bank"})
    assert d.local_only and "accounting" in d.reasons[0]
    assert pr.decide("Is card 4111 1111 1111 1111 on file?", {"documents"}).local_only
    assert PrivacyRouter(_policy(allow_cloud=False)).decide("lease?", {"documents"}).local_only
    assert not PrivacyRouter(_policy(cloud_allowed=frozenset({"documents", "accounting", "bank"}))).decide(
        "overdue?", {"accounting", "bank"}).local_only


def test_classification_of_files_and_tables():
    pol = _policy(path_rules=[("hr/", "personal")])
    assert pol.classify_file("invoices/summit.pdf") == "invoices"
    assert pol.classify_file("tables/bank_statement.xlsx") == "bank"
    assert pol.classify_file("hr/contract.pdf") == "personal"
    assert pol.classify_file("documents/lease.docx") == "documents"
    assert pol.classify_table("qbo_bills") == "accounting" and pol.classify_table("invoices") == "accounting"
    assert pol.classify_table("bank_statement_2026_q2") == "bank" and pol.classify_table("sales") == "documents"


def test_toolbox_guard_blocks_sensitive_data_for_cloud_models(store, retriever):
    store.load_dataframe("qbo_bills", __import__("pandas").DataFrame([{"id": "1", "total": 5.0}]))
    guard = PrivacyGuard(_policy())
    tb = ToolBox(store, retriever, privacy=guard)
    guard.cloud = True
    blocked = tb.run("run_sql", {"sql": "SELECT * FROM qbo_bills"})
    assert "blocked by privacy policy" in blocked["error"]
    assert "rows" in tb.run("run_sql", {"sql": "SELECT * FROM sales"})
    guard.cloud = False  # a local model may read it
    assert tb.run("run_sql", {"sql": "WITH x AS (SELECT * FROM qbo_bills) SELECT * FROM x"})["row_count"] == 1


def test_memory_withholds_local_only_turns_from_cloud():
    mem = ConversationMemory()
    mem.add("s", "user", "overdue?", local_only=True)
    mem.add("s", "assistant", "Oakridge owes 6,200", local_only=True)
    mem.add("s", "user", "lease?")
    assert [m["content"] for m in mem.history("s", for_cloud=True)] == [WITHHELD, WITHHELD, "lease?"]
    assert mem.history("s")[1]["content"] == "Oakridge owes 6,200"


# --------------------------------------------------------------------------- type-safe layer
def test_tool_schemas_generated_and_args_validated(store, retriever):
    spec = tool_spec("propose_action")["function"]
    assert spec["parameters"]["properties"]["action_type"]["enum"] == ["draft_email", "record_bill", "follow_up_task", "other"]
    assert "title" not in spec["parameters"]  # provider-friendly schema
    tb = ToolBox(store, retriever)
    assert "invalid arguments" in tb.run("run_sql", {})["error"]
    assert "invalid arguments" in tb.run("run_sql", {"sql": "SELECT 1", "extra": 1})["error"]
    assert "not available" in tb.run("propose_action", {"action_type": "other", "title": "x"})["error"]  # no approvals
    assert len(tb.run("search_docs", {"query": "baseline", "k": 99})["results"]) <= 10  # clamped, not rejected
    limited = ToolBox(store, retriever, allowed_tools=("search_docs",))
    assert "not available" in limited.run("run_sql", {"sql": "SELECT 1"})["error"]


class _Out(BaseModel):
    n: int


def test_structured_generation_retries_with_validation_errors():
    m = Model("x:y", True, json_replies=['not json', '```json\n{"n": "abc"}\n```', 'Sure: {"n": 3}'])
    assert generate(m, _Out, "sys", "count", retries=2).n == 3
    with pytest.raises(StructuredOutputError):
        generate(Model("x:y", True, json_replies=["{}", "{}"]), _Out, "sys", "count", retries=1)
    assert extract_json('noise {"a": 1} tail') == {"a": 1}


def test_invoice_schema_coerces_and_nullifies():
    f = InvoiceFields.model_validate({"invoice_number": "Not provided", "total": "$1,234.50", "tax": "N/A", "supplier": 123})
    assert f.invoice_number is None and f.total == 1234.5 and f.tax is None and f.supplier == "123"


# --------------------------------------------------------------------------- engine integration
def _engine(store, retriever, router, policy=None, approvals=None):
    return AgentEngine(store=store, retriever=retriever, guard=InputGuard(), llm=None, memory=ConversationMemory(),
                       router=router, intents=IntentRouter(model_fallback=False),
                       privacy=PrivacyRouter(policy or _policy()), approvals=approvals)


def test_sensitive_question_goes_local_even_when_cloud_is_first(store, retriever):
    cloud = Model("openai:gpt-4o", False, "cloud answer")
    local = Model("ollama:qwen2.5:14b", True, "local answer")
    engine = _engine(store, retriever, ModelRouter({"strong": [cloud, local], "fast": [cloud, local]}))
    out = engine.answer("s", "Which customers have overdue balances?")
    assert out["text"] == "local answer" and not cloud.seen
    r = out["routing"]
    assert r["intent"] == "accounting" and r["model"] == "ollama:qwen2.5:14b" and r["privacy"]["local_only"]


def test_document_question_uses_cloud_with_masking(store, retriever):
    cloud = Model("openai:gpt-4o-mini", False, "Baseline means expected units [guide.md].")
    engine = _engine(store, retriever, ModelRouter({"fast": [cloud]}))
    out = engine.answer("s", "What does baseline mean? Reply to dana@example.com")
    sent = cloud.seen[0]
    assert "dana@example.com" not in sent["question"] and "[EMAIL]" in sent["question"]
    assert sent["tools"] == ["search_docs"]
    assert out["routing"]["model_local"] is False and out["routing"]["privacy"]["redactions"] >= 1
    assert out["checks"] == {"citations": 1, "unverified_citations": []}


def test_sensitive_request_without_local_model_is_refused_honestly(store, retriever):
    cloud = Model("openai:gpt-4o", False, "should not be called")
    out = _engine(store, retriever, ModelRouter({"strong": [cloud]})).answer("s", "Which customers have overdue balances?")
    assert out["route"] == "extractive" and "must stay on this machine" in out["text"] and not cloud.seen


def test_unverified_citation_is_flagged(store, retriever):
    m = Model("ollama:x", True, "Per [Board_Minutes_2025.pdf, p.3] baseline is 5.")
    out = _engine(store, retriever, ModelRouter({"fast": [m]})).answer("s", "What does baseline mean?")
    assert out["checks"]["unverified_citations"] == ["Board_Minutes_2025.pdf, p.3"]


def test_cloud_follow_up_does_not_see_local_only_answer(store, retriever):
    cloud = Model("openai:gpt-4o-mini", False, "Baseline means expected units [guide.md].")
    local = Model("ollama:q", True, "Oakridge owes 6,200.")
    engine = _engine(store, retriever, ModelRouter({"fast": [cloud, local], "strong": [cloud, local]}))
    engine.answer("s", "Which customers have overdue balances?")
    engine.answer("s", "What does baseline mean?")
    assert all("6,200" not in h["content"] for h in cloud.seen[-1]["history"])


def test_fallback_to_next_model_is_reported(store, retriever):
    down = Model("anthropic:claude-haiku-4-5-20251001", False, RuntimeError("503"))
    up = Model("openai:gpt-4o-mini", False, "Baseline means expected units [guide.md].")
    out = _engine(store, retriever, ModelRouter({"fast": [down, up]})).answer("s", "What does baseline mean?")
    assert out["routing"]["fallbacks"] == 1 and out["routing"]["model"] == "openai:gpt-4o-mini"


def test_same_model_in_both_tiers_is_tried_once():
    m = Model("openai:x", True, RuntimeError("down"))
    router = ModelRouter({"fast": [m], "strong": [Model("openai:x", True)]})
    assert [c.name for c in router.candidates("strong")] == ["openai:x"]


def test_only_top_sensitive_passage_forces_local():
    from types import SimpleNamespace as P

    pr = PrivacyRouter(_policy())
    tail = [P(file="documents/lease.docx"), P(file="invoices/a.pdf")]
    assert not pr.decide("lease?", {"documents"}, tail, has_local=True).local_only
    top = [P(file="invoices/a.pdf"), P(file="documents/lease.docx")]
    assert pr.decide("coastal fee?", {"documents"}, top, has_local=True).local_only
    assert not pr.decide("coastal fee?", {"documents"}, top, has_local=False).local_only  # withheld instead

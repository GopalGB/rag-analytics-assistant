"""Semantic embeddings (cache, fallback), MMR diversity, reranking, provider streaming, and safe SSE snapshots."""

from __future__ import annotations

import json

import numpy as np

from app.agent.engine import AgentEngine
from app.agent.memory import ConversationMemory
from app.agent.streaming import stream_answer
from app.agent.tools import ToolBox
from app.llm.providers import AnthropicLLM, OpenAICompatLLM
from app.rag.embeddings import EmbeddingService
from app.rag.retriever import Chunk, Retriever
from app.security import InputGuard
from app.security.output_filter import register_secret


class Resp:
    def __init__(self, body=None, status=200, lines=None):
        self._body, self.status_code, self._lines = body, status, lines or []
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        return self._body

    def iter_lines(self, decode_unicode=True):
        yield from self._lines


class FakeEmbedHTTP:
    """Deterministic 'semantic' vectors: a few concept dimensions keyed on words."""

    CONCEPTS = [("lease", "tenancy", "landlord", "rent"), ("terminate", "cancel", "end", "notice"),
                ("invoice", "bill", "payment"), ("fire", "safety", "inspection")]

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls += 1
        if self.fail:
            return Resp({"error": "down"}, 503)
        data = []
        for i, text in enumerate(json["input"]):
            t = text.lower()
            vec = [float(sum(w in t for w in group)) + 0.01 for group in self.CONCEPTS]
            data.append({"index": i, "embedding": vec})
        return Resp({"data": data})


def _svc(tmp_path, http, **kw):
    return EmbeddingService("openai", openai_base_url="http://127.0.0.1:9/v1", openai_model="m", cache_dir=tmp_path,
                            http=http, **kw)


def test_semantic_embeddings_are_cached_on_disk(tmp_path):
    http = FakeEmbedHTTP()
    svc = _svc(tmp_path, http)
    svc.embed_documents(["the lease ends", "invoice paid"])
    assert http.calls == 1 and list(tmp_path.glob("*.npz"))
    http2 = FakeEmbedHTTP()
    svc2 = _svc(tmp_path, http2)
    svc2.embed_documents(["the lease ends", "invoice paid", "fire door inspection"])
    assert http2.calls == 1  # only the new text was sent
    assert svc2.name == "openai:m" and svc2.is_local


def test_semantic_failure_falls_back_whole_index(tmp_path):
    svc = _svc(tmp_path, FakeEmbedHTTP(fail=True))
    mat = svc.embed_documents(["a", "b"])
    assert svc.active == "local" and "unavailable" in svc.degraded and mat.shape[1] == svc.dim
    assert svc.embed_query("a") is not None  # same (hashing) space as the index


def test_semantic_search_finds_paraphrase(tmp_path):
    chunks = [Chunk("lease.docx", 0, "The tenant may give notice to end the tenancy after five years."),
              Chunk("policy.md", 1, "Invoices over 5,000 need director approval."),
              Chunk("project.pdf", 2, "Fire door inspection certificate is overdue.")]
    r = Retriever(_svc(tmp_path, FakeEmbedHTTP())).build(chunks)
    assert r.search("how can we cancel the rental with the landlord?", k=1)[0].file == "lease.docx"


def test_mmr_pushes_near_duplicates_down():
    emb = EmbeddingService("local", dim=256)
    dup = "Summit Ridge invoice INV-10421 total 4,871.25 copper cable LED panels"
    chunks = [Chunk("a.pdf", 0, dup), Chunk("a_copy.pdf", 0, dup + " copy"),
              Chunk("agreement.pdf", 0, "Summit Ridge supply agreement copper cable pricing and payment terms")]
    q = "Summit Ridge invoice INV-10421 copper cable"
    plain = Retriever(emb, mmr_lambda=1.0).build(chunks).search(q, k=2)
    diverse = Retriever(emb, mmr_lambda=0.5).build(chunks).search(q, k=2)
    assert {c.file for c in plain} == {"a.pdf", "a_copy.pdf"}
    assert "agreement.pdf" in {c.file for c in diverse}


def test_reranker_reorders_and_never_breaks_search():
    emb = EmbeddingService("local", dim=64)
    r = Retriever(emb).build([Chunk("x", 0, "alpha beta"), Chunk("y", 1, "alpha gamma")])
    r.reranker = lambda q, hits: [1, 0]
    first = r.search("alpha", k=2)
    r.reranker = lambda q, hits: (_ for _ in ()).throw(RuntimeError("model down"))
    assert len(r.search("alpha", k=2)) == 2 and len(first) == 2
    assert np.isfinite([c.score for c in first]).all()


# --------------------------------------------------------------------------- provider streaming
def _sse(*objs):
    return [f"data: {json.dumps(o)}" for o in objs] + ["data: [DONE]"]


class SeqHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    def post(self, url, headers=None, json=None, timeout=None, stream=False):
        self.bodies.append(json)
        return self.responses.pop(0)


def test_openai_stream_assembles_tool_call_deltas_then_streams_answer(store, retriever):
    tool_turn = _sse(
        {"choices": [{"delta": {"content": "Let me check. "}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "run_sql", "arguments": '{"sql": "SELECT reg'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ion FROM sales"}'}}]}}]},
    )
    answer_turn = _sse({"choices": [{"delta": {"content": "North "}}]}, {"choices": [{"delta": {"content": "and South."}}]},
                       {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 4}})
    http = SeqHTTP(Resp(lines=tool_turn), Resp(lines=answer_turn))
    events = []
    llm = OpenAICompatLLM("k", "http://127.0.0.1:1/v1", "m", http=http)
    tb = ToolBox(store, retriever)
    out = llm.converse("sys", [], "regions?", tb, 3, stream=lambda k, d=None: events.append((k, d)))
    assert out == "North and South."
    assert tb.last_sql == "SELECT region FROM sales"
    assert events[0] == ("text", "Let me check. ") and ("reset", None) in events
    assert events[-2:] == [("text", "North "), ("text", "and South.")]
    assert http.bodies[0]["stream"] is True


def test_anthropic_stream_events(store, retriever):
    turn1 = _sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "t1", "name": "search_docs"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"query": "base'}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": 'line"}'}},
        {"type": "message_delta", "usage": {"output_tokens": 5}},
    )
    turn2 = _sse(
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Baseline is "}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "expected units."}},
    )
    http = SeqHTTP(Resp(lines=turn1), Resp(lines=turn2))
    events = []
    tb = ToolBox(store, retriever)
    out = AnthropicLLM("k", "claude", http=http).converse("s", [], "baseline?", tb, 3,
                                                          stream=lambda k, d=None: events.append((k, d)))
    assert out == "Baseline is expected units."
    assert tb.sources and tb.sources[0]["file"] == "guide.md"
    assert [e for e in events if e[0] == "text"] == [("text", "Baseline is "), ("text", "expected units.")]
    assert http.bodies[1]["messages"][-1]["content"][0]["type"] == "tool_result"


# --------------------------------------------------------------------------- SSE safety
class StreamingModel:
    name, is_local, provider, model = "local:stream", True, "local", "stream"

    def __init__(self, chunks):
        self.chunks = chunks

    def converse(self, system, history, question, toolbox, max_iters, stream=None):
        for c in self.chunks:
            if stream:
                stream("text", c)
        return "".join(self.chunks)


def _engine(store, retriever, llm):
    return AgentEngine(store=store, retriever=retriever, guard=InputGuard(), llm=llm, memory=ConversationMemory())


def test_stream_events_and_secret_never_leaks(store, retriever):
    secret = "tenant-portal-key-9f8e7d6c5b4a"
    register_secret(secret)
    chunks = ["Baseline means ", "expected units. ", "The key is ", secret[:10], secret[10:], " and more text follows ",
              "so the stream keeps going for a while [guide.md]."]
    events = list(stream_answer(_engine(store, retriever, StreamingModel(chunks)), "s", "What does baseline mean?"))
    kinds = [e["type"] for e in events]
    assert kinds[:3] == ["status", "route", "model"] and kinds[-1] == "done"
    texts = [e["text"] for e in events if e["type"] == "text"]
    assert all(secret[:12] not in t for t in texts)
    final = events[-1]["payload"]
    assert secret not in final["text"] and "[redacted]" in final["text"]
    assert final["routing"]["model"] == "local:stream"


def test_stream_reports_fallback_with_reset(store, retriever):
    from app.llm.router import ModelRouter

    class Flaky(StreamingModel):
        name = "local:flaky"

        def converse(self, *a, stream=None, **k):
            stream("text", "partial answer that ")
            raise TimeoutError("stalled")

    router = ModelRouter({"fast": [Flaky([]), StreamingModel(["Baseline is expected units [guide.md]."])],
                          "strong": []})
    engine = AgentEngine(store=store, retriever=retriever, guard=InputGuard(), llm=None, memory=ConversationMemory(),
                         router=router)
    events = list(stream_answer(engine, "s", "What does baseline mean?"))
    kinds = [e["type"] for e in events]
    assert "reset" in kinds and kinds.count("model") == 2
    assert events[-1]["payload"]["routing"]["fallbacks"] == 1

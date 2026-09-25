"""Model router: pick a model per request, fall back on failure, and measure everything.

Tiers
    fast    cheap/quick model — document Q&A, classification, invoice field extraction
    strong  best model — accounting/SQL questions, drafting, multi-step reasoning
Each tier is an ordered fallback chain (e.g. `anthropic:claude-sonnet-5, openai:gpt-4o, ollama:qwen2.5:32b`).
If a tier has no usable model, the other tier's chain is used.

Per call the router
- filters the chain to LOCAL models when the privacy router says the data must stay on the machine;
- skips models whose circuit breaker is open (N consecutive failures → cooled down for M seconds);
- tries each candidate in order until one succeeds, recording latency, tokens and estimated cost;
- if EVERY candidate was rate limited and the providers said when to retry, waits that long (up to
  `rate_limit_max_wait` seconds) and makes one more pass, instead of failing a free-tier burst;
- returns the result plus a trace (which models were tried, which answered, why others failed).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.llm import usage
from app.llm.providers import BaseLLM
from app.llm.schemas import CallRecord

R = TypeVar("R")
TIERS = ("fast", "strong")


class NoModelAvailable(RuntimeError):
    def __init__(self, message: str, attempts: list[CallRecord] | None = None):
        super().__init__(message)
        self.attempts = attempts or []


@dataclass
class _Stats:
    calls: int = 0
    failures: int = 0
    total_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    consecutive_failures: int = 0
    open_until: float = 0.0
    last_error: str | None = None


@dataclass
class RouteTrace:
    tier: str
    local_only: bool
    purpose: str
    attempts: list[CallRecord] = field(default_factory=list)

    @property
    def model(self) -> CallRecord | None:
        return next((a for a in reversed(self.attempts) if a.ok), None)

    def to_dict(self) -> dict[str, Any]:
        ok = self.model
        return {
            "tier": self.tier,
            "local_only": self.local_only,
            "purpose": self.purpose,
            "model": ok.model if ok else None,
            "model_local": ok.local if ok else None,
            "fallbacks": sum(1 for a in self.attempts if not a.ok),
            "attempts": [a.model_dump() for a in self.attempts],
        }


def parse_pricing(value: str | None) -> dict[str, tuple[float, float]]:
    """LLM_PRICING='{"anthropic:claude-sonnet-5": [3, 15], ...}' = USD per million input/output tokens."""
    if not value:
        return {}
    try:
        raw = json.loads(value)
        return {k: (float(v[0]), float(v[1])) for k, v in raw.items()}
    except (ValueError, TypeError, IndexError):
        return {}


class ModelRouter:
    def __init__(
        self,
        tiers: dict[str, list[BaseLLM]],
        notes: list[str] | None = None,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        pricing: dict[str, tuple[float, float]] | None = None,
        on_call: Callable[[CallRecord, str], Any] | None = None,
        rate_limit_max_wait: float = 0.0,
    ):
        self.tiers = {t: list(tiers.get(t, [])) for t in TIERS}
        self.notes = notes or []
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.pricing = pricing or {}
        self.on_call = on_call
        self.rate_limit_max_wait = rate_limit_max_wait
        self.sleep: Callable[[float], Any] = time.sleep
        self._stats: dict[str, _Stats] = {}
        self._lock = threading.Lock()

    @classmethod
    def single(cls, llm: BaseLLM | None) -> ModelRouter:
        return cls({"fast": [llm], "strong": [llm]} if llm is not None else {})

    # ---- inventory ---------------------------------------------------------
    def models(self) -> list[BaseLLM]:
        seen: dict[str, BaseLLM] = {}
        for t in TIERS:
            for m in self.tiers[t]:
                seen.setdefault(m.name, m)
        return list(seen.values())

    @property
    def available(self) -> bool:
        return bool(self.models())

    @property
    def primary(self) -> BaseLLM | None:
        chain = self.tiers["strong"] or self.tiers["fast"]
        return chain[0] if chain else None

    def has_local(self) -> bool:
        return any(getattr(m, "is_local", False) for m in self.models())

    def has_cloud(self) -> bool:
        return any(not getattr(m, "is_local", False) for m in self.models())

    def _open(self, name: str) -> bool:
        st = self._stats.get(name)
        return bool(st and st.open_until > time.time())

    def candidates(self, tier: str, local_only: bool = False) -> list[BaseLLM]:
        chain = self.tiers.get(tier) or []
        other = self.tiers["strong" if tier == "fast" else "fast"]
        pool: list[BaseLLM] = []
        for m in chain + other:  # the tier's chain first, then the other tier as a last resort; no repeats
            if m.name not in {p.name for p in pool}:
                pool.append(m)
        if local_only:
            pool = [m for m in pool if getattr(m, "is_local", False)]
        healthy = [m for m in pool if not self._open(m.name)]
        return healthy or pool  # if every breaker is open, still try rather than refuse

    # ---- execution -----------------------------------------------------------
    def _cost(self, llm: BaseLLM, u: usage.Usage) -> float | None:
        price = self.pricing.get(llm.name) or self.pricing.get(getattr(llm, "model", ""))
        if not price:
            return 0.0 if getattr(llm, "is_local", False) else None
        return round((u.input_tokens * price[0] + u.output_tokens * price[1]) / 1_000_000, 6)

    def _record(self, llm: BaseLLM, ok: bool, ms: int, u: usage.Usage, error: str | None) -> CallRecord:
        cost = self._cost(llm, u)
        rec = CallRecord(model=llm.name, local=bool(getattr(llm, "is_local", False)), ok=ok, ms=ms, error=error,
                         input_tokens=u.input_tokens, output_tokens=u.output_tokens, cost_usd=cost)
        with self._lock:
            st = self._stats.setdefault(llm.name, _Stats())
            st.calls += 1
            st.total_ms += ms
            st.input_tokens += u.input_tokens
            st.output_tokens += u.output_tokens
            st.cost_usd += cost or 0.0
            if ok:
                st.consecutive_failures = 0
            else:
                st.failures += 1
                st.consecutive_failures += 1
                st.last_error = error
                if st.consecutive_failures >= self.failure_threshold:
                    st.open_until = time.time() + self.cooldown_seconds
        return rec

    def run(self, tier: str, fn: Callable[[BaseLLM], R], local_only: bool = False,
            purpose: str = "answer") -> tuple[R, RouteTrace]:
        trace = RouteTrace(tier=tier, local_only=local_only, purpose=purpose)
        cands = self.candidates(tier, local_only)
        if not cands:
            raise NoModelAvailable("no local AI model is available" if local_only else "no AI model is configured")
        waited = False
        while True:
            waits: list[float | None] = []
            for llm in cands:
                t0 = time.perf_counter()
                with usage.capture() as u:
                    try:
                        result = fn(llm)
                    except Exception as exc:  # any failure → next model
                        rec = self._record(llm, False, int((time.perf_counter() - t0) * 1000), u,
                                           f"{type(exc).__name__}: {str(exc)[:200]}")
                        trace.attempts.append(rec)
                        if self.on_call:
                            self.on_call(rec, purpose)
                        waits.append(getattr(exc, "retry_after", None) if getattr(exc, "status", None) == 429 else None)
                        continue
                rec = self._record(llm, True, int((time.perf_counter() - t0) * 1000), u, None)
                trace.attempts.append(rec)
                if self.on_call:
                    self.on_call(rec, purpose)
                return result, trace
            wait = min(waits) if waits and None not in waits else None
            if waited or wait is None or wait > self.rate_limit_max_wait:
                break
            self.sleep(wait)  # every model is briefly rate limited: wait as asked, then one more pass
            waited = True
        raise NoModelAvailable(f"all {len(cands)} candidate model(s) failed", trace.attempts)

    def bound(self, tier: str, local_only: bool = False, purpose: str = "task") -> RoutedLLM | None:
        """An object with complete/complete_json that routes each call (for extraction etc.)."""
        return RoutedLLM(self, tier, local_only, purpose) if self.candidates(tier, local_only) else None

    # ---- reporting -------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        def row(m: BaseLLM) -> dict[str, Any]:
            st = self._stats.get(m.name) or _Stats()
            return {
                "model": m.name,
                "provider": getattr(m, "provider", ""),
                "local": bool(getattr(m, "is_local", False)),
                "calls": st.calls,
                "failures": st.failures,
                "avg_ms": int(st.total_ms / st.calls) if st.calls else None,
                "input_tokens": st.input_tokens,
                "output_tokens": st.output_tokens,
                "cost_usd": round(st.cost_usd, 4),
                "priced": bool(self.pricing.get(m.name) or self.pricing.get(getattr(m, "model", ""))),
                "circuit_open": self._open(m.name),
                "last_error": st.last_error,
            }

        return {
            "tiers": {t: [m.name for m in self.tiers[t]] for t in TIERS},
            "models": [row(m) for m in self.models()],
            "notes": self.notes,
        }


class RoutedLLM:
    """Adapter so code expecting one LLM (e.g. invoice extraction) gets routing + fallback for free."""

    supports_tools = False

    def __init__(self, router: ModelRouter, tier: str, local_only: bool, purpose: str):
        self.router, self.tier, self.local_only, self.purpose = router, tier, local_only, purpose
        first = router.candidates(tier, local_only)
        self.name = f"router:{tier}" + (" (local only)" if local_only else "")
        self.is_local = all(getattr(m, "is_local", False) for m in first)
        self.last_trace: RouteTrace | None = None

    def complete(self, system: str, prompt: str) -> str:
        out, self.last_trace = self.router.run(self.tier, lambda m: m.complete(system, prompt), self.local_only, self.purpose)
        return out

    def complete_json(self, system: str, prompt: str, schema: dict[str, Any], name: str = "result") -> str:
        def call(m: BaseLLM) -> str:
            if callable(getattr(m, "complete_json", None)):
                return m.complete_json(system, prompt, schema, name)
            return m.complete(system, prompt)

        out, self.last_trace = self.router.run(self.tier, call, self.local_only, self.purpose)
        return out

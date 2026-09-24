"""Observability: request IDs, structured JSON logs, and Prometheus-format metrics. No extra dependencies.

- Every request gets an `X-Request-ID` (accepted from the caller if well-formed, else generated). It is
  returned in the response, attached to every log line written while handling the request, and
  recorded in the activity log via the engine, so one ID ties together UI → API → model calls.
- Logs are one JSON object per line on stderr (LOG_FORMAT=json, default) or plain text (LOG_FORMAT=text).
  Question text and document content are never logged here — only IDs, routes, timings and status.
- `/metrics` exposes counters and histograms in the Prometheus text format (requests, latency, model
  calls, tokens, cost, fallbacks), so any standard monitoring stack can scrape it.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import secrets
import sys
import threading
import time
from collections import defaultdict
from typing import Any

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_RID_OK = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

log = logging.getLogger("assistant")


# --------------------------------------------------------------------------- logging
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        for k, v in getattr(record, "fields", {}).items():
            entry[k] = v
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = " ".join(f"{k}={v}" for k, v in getattr(record, "fields", {}).items())
        base = f"{time.strftime('%H:%M:%S')} {record.levelname:<7} [{request_id_var.get()}] {record.getMessage()} {fields}"
        return base + ("\n" + self.formatException(record.exc_info) if record.exc_info else "")


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger("assistant")
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    root.propagate = False


def event(msg: str, level: int = logging.INFO, **fields: Any) -> None:
    log.log(level, msg, extra={"fields": fields})


# --------------------------------------------------------------------------- metrics
_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[tuple[str, tuple], float] = defaultdict(float)
        self.hist: dict[tuple[str, tuple], list[float]] = {}
        self.help: dict[str, tuple[str, str]] = {}
        self.started = time.time()

    def _key(self, name: str, labels: dict[str, Any]) -> tuple[str, tuple]:
        return name, tuple(sorted((k, str(v)) for k, v in labels.items()))

    def inc(self, name: str, value: float = 1.0, help: str = "", **labels: Any) -> None:
        with self._lock:
            self.help.setdefault(name, ("counter", help))
            self.counters[self._key(name, labels)] += value

    def observe(self, name: str, value: float, help: str = "", **labels: Any) -> None:
        with self._lock:
            self.help.setdefault(name, ("histogram", help))
            h = self.hist.setdefault(self._key(name, labels), [0.0] * (len(_BUCKETS) + 2))  # buckets.., sum, count
            for i, b in enumerate(_BUCKETS):
                if value <= b:
                    h[i] += 1
            h[-2] += value
            h[-1] += 1

    @staticmethod
    def _labels(pairs: tuple, extra: tuple = ()) -> str:
        items = list(pairs) + list(extra)
        if not items:
            return ""
        esc = [(k, v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")) for k, v in items]
        return "{" + ",".join(f'{k}="{v}"' for k, v in esc) + "}"

    def render(self, gauges: dict[str, tuple[float, str]] | None = None) -> str:
        out: list[str] = []
        with self._lock:
            names = sorted(set(self.help))
            for name in names:
                kind, help_text = self.help[name]
                out += [f"# HELP {name} {help_text or name}", f"# TYPE {name} {kind}"]
                if kind == "counter":
                    for (n, labels), v in sorted(self.counters.items()):
                        if n == name:
                            out.append(f"{n}{self._labels(labels)} {v:g}")
                else:
                    for (n, labels), h in sorted(self.hist.items()):
                        if n != name:
                            continue
                        for i, b in enumerate(_BUCKETS):
                            out.append(f"{n}_bucket{self._labels(labels, (('le', f'{b:g}'),))} {h[i]:g}")
                        out.append(f"{n}_bucket{self._labels(labels, (('le', '+Inf'),))} {h[-1]:g}")
                        out.append(f"{n}_sum{self._labels(labels)} {h[-2]:.6f}")
                        out.append(f"{n}_count{self._labels(labels)} {h[-1]:g}")
        gauges = {"assistant_uptime_seconds": (time.time() - self.started, "Seconds since start"), **(gauges or {})}
        for name, (value, help_text) in gauges.items():
            out += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value:g}"]
        return "\n".join(out) + "\n"


METRICS = Metrics()


def record_model_call(rec: Any, purpose: str) -> None:
    """ModelRouter on_call hook: one CallRecord per attempt."""
    labels = {"model": rec.model, "local": str(rec.local).lower(), "purpose": purpose.split(":")[0]}
    METRICS.inc("assistant_llm_calls_total", help="Model calls by model, locality, purpose and outcome",
                outcome="ok" if rec.ok else "error", **labels)
    METRICS.observe("assistant_llm_call_seconds", rec.ms / 1000, help="Model call latency", **labels)
    METRICS.inc("assistant_llm_tokens_total", rec.input_tokens, help="Tokens by direction", direction="input",
                model=rec.model)
    METRICS.inc("assistant_llm_tokens_total", rec.output_tokens, direction="output", model=rec.model)
    if rec.cost_usd:
        METRICS.inc("assistant_llm_cost_usd_total", rec.cost_usd, help="Estimated model cost (LLM_PRICING)", model=rec.model)
    event("model_call", model=rec.model, local=rec.local, ok=rec.ok, ms=rec.ms, purpose=purpose,
          tokens_in=rec.input_tokens, tokens_out=rec.output_tokens, error=rec.error)


# --------------------------------------------------------------------------- ASGI middleware
class RequestContextMiddleware:
    """Pure ASGI (streaming-safe): request ID, access log, latency + status metrics."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _RID_OK.match(incoming) else secrets.token_hex(8)
        token = request_id_var.set(rid)
        start = time.perf_counter()
        status = {"code": 500}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - start
            route = scope.get("route")
            path = getattr(route, "path", None) or ("unmatched" if status["code"] == 404 else scope.get("path", ""))
            method = scope.get("method", "")
            METRICS.inc("assistant_http_requests_total", help="HTTP requests", method=method, path=path,
                        status=str(status["code"]))
            METRICS.observe("assistant_http_request_seconds", elapsed, help="HTTP request latency", method=method,
                            path=path)
            if path not in ("/health", "/ready", "/metrics") and not path.startswith("/ui/"):
                event("request", method=method, path=path, status=status["code"], ms=round(elapsed * 1000, 1))
            request_id_var.reset(token)

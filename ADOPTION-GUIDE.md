# Adoption Guide

This guide maps the improvements demonstrated in this reference repo onto **any existing**
FastAPI + DuckDB RAG chatbot. Each item is generic: read the "Reference" file here, then apply the
same pattern to the equivalent file in your project. Apply them top-to-bottom; run your tests after each.

| # | Problem | Fix | Reference in this repo |
|---|---------|-----|------------------------|
| 1 | LLM is optional garnish on a large hand-written intent parser → brittle "normal chatbot" | Make the LLM the primary brain via a **tool-calling loop**; demote deterministic logic to a fallback | `app/agent/engine.py`, `app/agent/fallback.py` |
| 2 | "Planner" returns a hand-parsed JSON blob → silent failures on malformed JSON | Use **native tool-calling** with a typed tool schema | `app/agent/tools.py`, `app/agent/llm.py` |
| 3 | No multi-turn memory → can't handle follow-ups | Add per-session **conversation memory** | `app/agent/memory.py` |
| 4 | Vectors stored as JSON and scanned row-by-row in Python → won't scale | Dense matrix + single matmul; swap to an ANN index for large corpora | `app/rag/vectors.py` |
| 5 | Table reload drops the table before recreating → data loss on failure | **Atomic load**: build a temp table, then swap | `app/data/store.py` (`load_csv`) |
| 6 | Dead modules import things that don't exist | Delete dead code or restore the missing modules — keep imports honest | n/a (hygiene) |
| 7 | Unpinned dependencies → "works on my machine" | Pin ranges + commit a lockfile | `requirements.txt`, `requirements.lock.txt` |
| 8 | Two UIs, one orphaned | Keep one wired UI; delete the rest | `app/ui/chat.html` |
| 9 | Tests exist but no coverage/CI | Add CI that lints + tests on every push | `.github/workflows/ci.yml`, `tests/` |
| 10 | Errors can leak internals | Global handler returns a generic 500 | `app/main.py` |
| 11 | (Keep!) good input firewall + safe SELECT + output scrub | Preserve and extend, don't weaken | `app/security/`, `app/data/store.py` |
| 12 | A placeholder/guessed model id | Set a **real** model id you have access to; never ship a guess | `app/config.py` (`openai_model`) |

## Detail on the headline change (item 1 + 2): LLM-first agent

**Before (typical):** a 500–600 line intent parser generates SQL for pre-guessed questions; the LLM only
runs if the parser misses. Anything unanticipated falls through.

**After (this repo):** the engine builds a system prompt (armor + live schema + doc summary), then runs a
**bounded tool-calling loop**:

1. The model receives the question + tool definitions (`run_sql`, `search_docs`).
2. It calls a tool; the backend executes it **safely** and returns the result.
3. The model reads the result and either calls another tool or gives a final answer.
4. The loop is capped (`max_tool_iterations`); on exhaustion the model is asked for a final answer with no tools.

Deterministic logic doesn't disappear — it becomes the **safety boundary** (`app/data/store.run_select`
validates every query regardless of what the model proposes) and the **offline fallback**
(`app/agent/fallback.py`).

## Detail on SQL safety (item 11) — do NOT rely on a denylist

A denylist of forbidden function names (`read_csv`, `read_parquet`, …) is **not** a safe boundary:
it misses functions (`read_ndjson`, `read_duckdb`, … added across DuckDB versions), is evaded by
direct file paths (`SELECT * FROM '/etc/passwd'`), by SQL comments splitting tokens, and by
schema-qualified internal tables (`main._internal`). This reference uses three layers instead — see
`app/data/store.py`:

1. **Lockdown** — `SET enable_external_access=false` on the connection, so no query can read a file
   or the network regardless of phrasing. Ingestion reads CSVs in Python (pandas) and registers them
   in memory, so the engine never needs file access.
2. **Table allowlist** — parse the query with DuckDB's own parser (`json_serialize_sql`) and require
   every referenced table to be a loaded, non-internal table (or a CTE defined in the query).
3. **Hard row cap** — wrap the query in an outer `LIMIT` and `fetchmany`, so a missing or
   subquery-only `LIMIT` can't return or materialize unbounded rows (a DoS vector).

If your existing tool gates SQL with a substring denylist, replace it with these three layers.

## Recommended next (not yet in this reference)

- **Streaming responses (SSE)** for token-by-token UX. Add a `POST /chat/stream` endpoint that yields
  `text/event-stream` chunks; the provider client streams deltas. Keep the same guard + scrub around it.
- **ANN vector index** (sqlite-vec / DuckDB VSS / FAISS) behind the `VectorIndex` interface once the
  corpus outgrows an in-memory matrix.
- **Coverage gate** in CI (e.g. fail under 80%).

## Application checklist

- [ ] Map your real table/column names wherever this repo says `sales` / `revenue` / `region`.
- [ ] Keep your existing security layer; port any rule this repo has that yours lacks.
- [ ] Do **not** import this repo's synthetic data — keep your own data in place.
- [ ] Run your full test suite after each item; do not proceed on a red suite.

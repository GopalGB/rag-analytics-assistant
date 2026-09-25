# Deployment options

There are three ways to run the assistant. Only the first is the target for company data.

| Option | Where | Data | AI model | Use it for |
|---|---|---|---|---|
| **Native on the Mac Studio** (recommended) | the Mac, `make run` / launchd | real documents, after approval | local (Ollama, Metal GPU) | the real prototype; see [INSTALL-MAC.md](INSTALL-MAC.md) |
| **Docker** | the Mac or a Linux server | real or sample | Ollama on the host (or in compose) | servers; see [OPERATIONS.md](OPERATIONS.md#running-in-docker-optional) |
| **Hosted public demo** | Vercel + an optional Cloudflare Worker | **synthetic sample data only** | a hosted model (e.g. Groq) | showing the prototype to people without installing it |

## Hosted public demo (Vercel + Cloudflare Worker)

The public demo runs the same app with `PUBLIC_DEMO=true`, which makes it **read-only and stateless**:
uploads, re-scans, invoice reviews, proposing/approving actions and QuickBooks connect/sync/disconnect
all return `403`, and conversations are not remembered between questions. Reading everything works:
Overview, Ask (streaming), Documents and originals, Invoices, the **Invoice lab** (paste text → fields,
nothing stored), QuickBooks (offline test company), Reports, Activity log and Privacy.

> Questions, retrieved passages and tool results are sent to the hosted model provider. Deploy only the
> bundled synthetic data plus the public documents listed in [PUBLIC-DOCUMENTS.md](PUBLIC-DOCUMENTS.md);
> never real company files.

### Vercel (the app)

Vercel detects the FastAPI app in `app/main.py` and installs `requirements.txt` / `pyproject.toml`
(kept identical by a test). `.vercelignore` keeps tests, docs, local state and secrets out of the bundle.
Set these environment variables (secrets in Vercel's encrypted settings, never in the repo):

| Variable | Value | Why |
|---|---|---|
| `PUBLIC_DEMO` | `true` | read-only, no memory |
| `DB_PATH` | `:memory:` | the filesystem is read-only except `/tmp` |
| `STORAGE_DIR` | `/tmp/assistant` | caches and the (ephemeral) activity log |
| `AUTO_REINDEX` | `false` | no file watcher on a serverless function |
| `EMBEDDING_PROVIDER` | `local` | offline hashing vectors (no embedding server) |
| `ALLOW_CLOUD_AI` | `true` | the only model is a hosted one |
| `GROQ_API_KEY` | secret | or any other provider key |
| `LLM_MODELS_STRONG` / `LLM_MODELS_FAST` | `groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,groq:qwen/qwen3.8-27b` | a fallback chain: each free-tier model has its own quota |
| `INVOICE_AI_ASSIST` | `false` | the rules extractor already reads every sample invoice; skipping the AI pass keeps cold starts under a second and saves quota |
| `ANSWER_CACHE_SEED` | `data/demo_answer_cache.json` | pre-generated answers to the suggested questions (see below) |
| `RATE_LIMIT_MAX_WAIT_SECONDS` | `8` (default) | when every model is rate limited, wait once if the provider says it frees up within this many seconds |
| `CLOUD_ALLOWED_DATA` | `documents,invoices,accounting,bank` | acceptable **only** because every file is synthetic or public |
| `REDACT_PII` | `true` | still mask emails/phones/accounts |
| `APP_API_KEY` | secret | the Worker adds it; direct calls to the Vercel URL are refused |
| `TRUST_LOOPBACK` | `false` | never exempt anyone from the key behind a proxy |
| `ALLOWED_HOSTS` | `rag-analytics-assistant.vercel.app` | Host allowlist (DNS-rebinding defence) |
| `ALLOWED_ORIGINS` | `https://gopalbagaswar.com` | the browser origin the Worker serves the UI from |

**Answer cache.** The demo is stateless and read-only, so the same question always deserves the same
answer. Model answers are kept in memory (`ANSWER_CACHE_SIZE`, default 256) and repeats are served
instantly with a "cached answer" label; guardrails still run first, and refusals or fallbacks are never
cached. `scripts/build_answer_cache.py` asks every suggested question once through the real pipeline and
writes `data/demo_answer_cache.json` with a fingerprint of `data/sample/`; the seed is ignored if any
document changed. Regenerate it after changing documents, prompts or models:

```bash
PUBLIC_DEMO=true DB_PATH=:memory: EMBEDDING_PROVIDER=local ALLOW_CLOUD_AI=true INVOICE_AI_ASSIST=false \
CLOUD_ALLOWED_DATA=documents,invoices,accounting,bank REPORT_AS_OF=2026-07-15 GROQ_API_KEY=... \
LLM_MODELS_STRONG=... LLM_MODELS_FAST=... python scripts/build_answer_cache.py --pace-seconds 25
```

No OCR engine is available on Vercel, so the scanned sample invoice is flagged "could not be read"
there; everything else behaves as on the Mac.

### Cloudflare Worker (optional: serve it under your own domain)

`deploy/rag-assistant-worker.mjs` serves the app at `https://gopalbagaswar.com/rag-assistant/` without
touching the rest of the site. It:

- only handles `/rag-assistant` (redirected to `/rag-assistant/`) and `/rag-assistant/*`, and only
  GET/HEAD/POST/OPTIONS;
- forwards to the fixed HTTPS Vercel origin with the prefix removed, adding `x-api-key` from the
  `ORIGIN_API_KEY` Worker secret (must equal `APP_API_KEY` on Vercel); returns 503 if it is missing;
- strips `authorization`, `cookie`, `set-cookie`, client `x-api-key` and spoofed `x-forwarded-*` headers;
- rejects upstream redirects and turns upstream 5xx or network errors into a generic 502, while passing
  4xx responses (e.g. 429 rate limits) through unchanged.

The UI builds every URL relative to the page, so it works under the `/rag-assistant/` prefix.
Configuration is in `deploy/wrangler.toml`; set the secret with `npx wrangler secret put ORIGIN_API_KEY`
and deploy with `npx wrangler deploy` from `deploy/`. Check that the route doesn't conflict with other
Workers on the zone before deploying.

Tests: `node --test tests/*.mjs` (also run in CI).

### Checking a deployment

```bash
python scripts/evaluate_live.py --base-url https://gopalbagaswar.com/rag-assistant/ --runs 20 \
    --pace-seconds 20 --output evaluation.json      # pace for a free-tier model quota
```

The live evaluator checks health, that the documents load and every original downloads, invoices and
their flags, the Invoice lab, the dashboard, the project report's discrepancy, and chat answers
(citations, "not found", a refused prompt injection, and table/accounting questions when a model is
present). It reports cold and warm p50/p95 latency; add `--max-p95-ms 5000` to enforce a target.

**Status:** the configuration and proxy are tested locally (unit tests and the live evaluator against a
local server). This repository makes no claim that the demo is deployed or of its latency on Vercel;
run the evaluator above after deploying.

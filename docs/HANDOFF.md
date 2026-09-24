# Handoff: take this repo and deploy it

Everything is built and tested. What's left is yours to add: API keys, hosting accounts and (later) real
documents. Nothing secret is in the repository, and nothing you add should be.

## 1. Get it running (5 minutes)

```bash
git clone <this repo> && cd rag-analytics-assistant
git checkout claude/project-prototype-end-to-end-rs8iur      # until the PR is merged
make setup            # venv + dependencies
make test             # 240+ Python tests + proxy tests (needs Node 18+ for the latter)
make demo             # starts on http://127.0.0.1:8000 with the sample company, as of 2026-07-15
```

Without any AI model it already works end to end: extraction, reconciliation, the attention list,
deadlines, reports and cited passages. Add a model next.

## 2. Add your keys (never committed)

```bash
cp env.example .env && chmod 600 .env
```

Pick one or more:

| You want | Put in `.env` |
|---|---|
| Local model on the Mac (recommended for company data) | `brew install ollama && ollama pull qwen2.5:14b && ollama pull nomic-embed-text`. No key needed. |
| A cloud model too | `ALLOW_CLOUD_AI=true` plus `ANTHROPIC_API_KEY=` / `OPENAI_API_KEY=` / `GROQ_API_KEY=` / ... Accounting, invoice and bank data still stay local unless you widen `CLOUD_ALLOWED_DATA`. |
| QuickBooks sandbox | `QBO_MODE=sandbox`, `QBO_CLIENT_ID=`, `QBO_CLIENT_SECRET=` ([QUICKBOOKS.md](QUICKBOOKS.md)) |
| Access from other machines | `APP_API_KEY=` (generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`), `ALLOWED_HOSTS=` with the Mac's name/IP |

`.env`, key files, `.vercel/`, `.wrangler/`, `storage/` and `backups/` are git-ignored. If you install the
optional hooks (`pip install pre-commit && pre-commit install`), gitleaks blocks any commit that contains a
credential; CI scans the whole history on every push.

## 3. Check before deploying

```bash
make preflight                        # local Mac install
make preflight TARGET=docker
make preflight TARGET=public-demo     # run with the demo's env vars set in your shell
```

It fails on anything unsafe (missing or short API key, a public demo that could see real files or isn't
read-only, `.env` committed, credential-shaped strings in tracked files) and never prints a secret.

## 4. Deploy

| Target | Steps |
|---|---|
| **Mac Studio** (company data) | [INSTALL-MAC.md](INSTALL-MAC.md): launchd autostart, FileVault, Ollama on the GPU |
| **Docker** | `make docker-up` (host Ollama) or `make docker-up-ollama` (Ollama in a container) |
| **Hosted public demo** | [DEPLOYMENT.md](DEPLOYMENT.md): Vercel project with the variables in `deploy/vercel.env.example` (secrets in Vercel's settings), then optionally the Cloudflare Worker in `deploy/` (`npx wrangler secret put ORIGIN_API_KEY`, `npx wrangler deploy`) |

## 5. Verify the deployment

```bash
python scripts/evaluate_live.py --base-url <url> --runs 20 --output evaluation.json
```

It checks documents, originals, invoices, the attention list, reports and chat answers (citations,
"not found", prompt-injection refusal, table questions) and reports p50/p95 latency. Add
`--max-p95-ms 5000` to enforce a target on the hardware you are accepting. `make evaluate` re-measures
extraction/search/reconciliation accuracy offline and fails if any acceptance threshold drops.

## 6. When you move to real documents

- Start with a small, non-confidential set in a new folder; set `DATA_DIR` to it. Never point the public
  demo at company files.
- Put the company's real approval policy in that folder: the attention list reads its thresholds from it.
- Set `REPORT_AS_OF` only for demos; leave it unset in real use (today's date).
- Back up with `make backup` (checksummed; `ARGS=--include-env` only onto encrypted storage).

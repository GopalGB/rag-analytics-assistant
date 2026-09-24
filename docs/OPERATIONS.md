# Operations

Local setup is make setup, make sample-data, make test, then configure a provider before make run. The
local server listens on 127.0.0.1:8000. Stop it with Ctrl-C. The evaluator command is:

    python scripts/evaluate_demo.py --base-url http://127.0.0.1:8000 --runs 20 --output evaluation.json

Environment variables include DB_PATH (default storage/analytics.duckdb), DATA_DIR (default data/sample),
AUTO_REINDEX (default true), PUBLIC_DEMO (default false),
REQUIRE_LLM, LLM_PROVIDER, OPENAI_API_KEY, OPENAI_MODEL, OPENAI_BASE_URL, EMBEDDING_PROVIDER,
OPENAI_EMBEDDING_MODEL, LOCAL_EMBEDDING_DIM, MAX_BODY_BYTES (65536), MAX_INPUT_CHARS (2000),
RATE_PER_MINUTE (60), RATE_BURST (20), APP_API_KEY, TRUST_LOOPBACK (true), MAX_TOOL_ITERATIONS (4),
MAX_SQL_ROWS (200), HISTORY_TURNS (8), REINDEX_INTERVAL_SECONDS (5), and QuickBooks connector settings.
Values and secrets belong in the environment or an untracked local file.

The shipped synthetic corpus has 15 physical files: one CSV, eight invoice examples, two PDFs, one DOCX,
and reference documents covering procurement, payment, and escalation. The runtime accepts CSV, Markdown/text,
text PDF, and DOCX. It does not perform OCR. DB_PATH=:memory: is ephemeral; a persistent DuckDB path needs
backup of that file while the application is stopped. Public mode has no private-data backup or QuickBooks
refresh path.

Controls include disabled DuckDB external access, parsed table allowlists, read-only SELECT enforcement,
body-size limits, bounded instance-local per-IP rate state, security headers, input firewall, and output
scrubbing. Known ceilings are provider availability, instance-local rate limits, no OCR, and no distributed
job or durable public conversation store. Troubleshooting starts with the server log, /health, and a check
that the configured provider and API key are available; no provider response is treated as a successful answer.

# Operating and maintenance guide

## Daily use

| Task | How |
|---|---|
| Add documents | Drop files into the data folder (`DATA_DIR`, default `data/sample`), or upload in **Documents**. New or changed files are picked up within ~5 seconds. **Re-scan data folder** forces it. |
| Review invoices | **Invoices** → open an invoice → correct any field → **Approve** or **Reject**. Enter your name first (top right). Corrections are saved locally and survive restarts. |
| Refresh QuickBooks | **QuickBooks → Sync now**. Data is copied locally (read-only); questions never trigger live calls. |
| Act on discrepancies | Use **Propose** on a reconciliation row, then approve or reject in **Approvals**. |
| See the numbers | **Overview**: KPIs, aging, spend, cash flow, budget vs. actual; every chart has a table view. |
| Reports | **Reports**: accounts summary, aging, outstanding items, project status. Download as Markdown or open the printable HTML (browser Print → PDF). **AI summary** adds a short model-written summary (privacy-routed: local only for accounting data). |
| Export | **Invoices → Export CSV**; **Reports → Download .md**. |
| Check activity | **Activity log**. The integrity badge confirms the log hasn't been altered. |

Supported files: PDF (text or scanned), DOCX, TXT/MD, PNG/JPG/TIFF (OCR), CSV/XLSX (become SQL tables;
first sheet of an XLSX).

## Where things are stored

Everything is in two folders on the Mac:

| Path | Contents |
|---|---|
| `DATA_DIR` (default `data/sample/`) | Your source documents. Uploads go to `uploads/` inside it. |
| `storage/assistant.duckdb` | Local database: spreadsheets, extracted invoices, QuickBooks copy, reconciliation |
| `storage/invoice_reviews.json` | Review decisions and corrections (keyed by file hash) |
| `storage/approvals.json` | Proposed actions and decisions |
| `storage/audit.jsonl` | Activity log (append-only, hash-chained) |
| `storage/cache/parsed/` | Parsed/OCR'd text cache, so unchanged files aren't re-processed |
| `storage/secrets/qbo_tokens.json` | QuickBooks OAuth tokens (file mode 0600), only in sandbox mode. Or the macOS Keychain with `SECRETS_BACKEND=keyring`. |

The database and cache can always be rebuilt from `DATA_DIR` and QuickBooks. **Back up** `DATA_DIR`,
`storage/invoice_reviews.json`, `storage/approvals.json`, `storage/audit.jsonl` and `.env`. Time
Machine to an encrypted disk is enough for a single Mac, or use the built-in backup:

```bash
make backup                                   # backups/assistant-backup-<time>.tar.gz with a sha256 manifest
make backup ARGS=--include-env                # also keep .env (contains keys — store it encrypted)
make verify-backup FILE=backups/assistant-backup-....tar.gz
make restore FILE=backups/assistant-backup-....tar.gz   # stop the app first; refuses to overwrite without --force
```

Caches (`storage/cache/`) are skipped; they are rebuilt on start.

## Monitoring

| Endpoint | Use |
|---|---|
| `GET /health` | Version, model status, config warnings (e.g. cloud AI on with PII masking off) |
| `GET /ready` | 200 once the index and tables are loaded; for launchd/Docker health checks |
| `GET /metrics` | Prometheus text: requests by route and status, latency, model calls, tokens, cost, fallbacks |

Every request gets an `X-Request-ID` (or keeps a valid one sent by a proxy). It appears in the response
header, every JSON log line (`LOG_FORMAT=json`), each model call and the activity log, so one question
can be traced end to end. Configuration problems are logged once at startup as `config_warning`.

## Running in Docker (optional)

```bash
make docker-up        # builds the image and starts it on 127.0.0.1:8000
```

The container runs as a non-root user with a read-only filesystem; `data/` and `storage/` are volumes.
Ollama stays on the Mac (it needs the GPU) and is reached at `host.docker.internal:11434`.

## Configuration

All settings live in `.env` (see `env.example`); restart after changes. The ones you're most likely to
touch: `DATA_DIR`, `OLLAMA_MODEL`, `OCR_LANG`, `DATE_ORDER` (`MDY` or `DMY`), `QBO_MODE`, `APP_API_KEY`.

## Updating

```bash
git pull
make setup          # picks up new dependencies
make test           # confirm everything passes on this machine
ollama pull qwen2.5:14b   # refresh the model (optional)
```

To try a different model, set `OLLAMA_MODEL` and run `python scripts/evaluate.py --with-ai` to compare
extraction results before switching.

## Revoking access

- **QuickBooks**: **QuickBooks → Disconnect & revoke** revokes the token at Intuit and deletes both the
  token and the local QuickBooks copy. You can also remove the app in QuickBooks under
  *Settings → Apps*.
- **Network access**: first stop the server, or restart it bound to `127.0.0.1` (the default), and only
  then remove `APP_API_KEY`. Removing the key while the server is still bound to `0.0.0.0` leaves it
  open to the network without authentication.
- **Cloud AI**: remove `ALLOW_CLOUD_AI` (default is blocked).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Header says "AI: off" | Start Ollama (`ollama serve`) and pull the model in `OLLAMA_MODEL`. The Privacy tab shows the exact reason. |
| "Quoted from sources · no AI" answers | Same as above. The model is unreachable, so the assistant quotes passages instead of guessing. |
| Scanned PDF shows "could not be read" | `brew install tesseract`, restart, **Re-scan**. |
| Wrong day/month on invoices | Set `DATE_ORDER=DMY` (ambiguous dates are always flagged for review either way). |
| Answers are slow | Use a smaller model (e.g. `qwen2.5:14b` instead of 32b), or lower `PREFETCH_PASSAGES`. Answers stream, so the first words appear quickly either way. |
| Search says "keyword + hashing" | `ollama pull nomic-embed-text`; the index switches to semantic vectors on the next re-scan. |
| Aging figures look wrong for the demo | The sample data is dated mid-2026; `make demo` sets `REPORT_AS_OF=2026-07-15`. |
| Start over for a demo | `make demo` (deletes `storage/` and uploads; keeps the sample data). |
| Server log | Terminal output, or `storage/server.log` when started by launchd. |

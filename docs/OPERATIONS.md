# Operating and maintenance guide

## Daily use

| Task | How |
|---|---|
| Add documents | Drop files into the data folder (`DATA_DIR`, default `data/sample`), or upload in **Documents**. New or changed files are picked up within ~5 seconds. **Re-scan data folder** forces it. |
| Review invoices | **Invoices** → open an invoice → correct any field → **Approve** or **Reject**. Enter your name first (top right). Corrections are saved locally and survive restarts. |
| Refresh QuickBooks | **QuickBooks → Sync now**. Data is copied locally (read-only); questions never trigger live calls. |
| Act on discrepancies | Use **Propose** on a reconciliation row, then approve or reject in **Approvals**. |
| Export | **Invoices → Export CSV**; **QuickBooks → Download** (draft accounts summary, Markdown). |
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
Machine to an encrypted disk is enough for a single Mac.

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
- **Network access**: remove `APP_API_KEY` / stop binding to `0.0.0.0`; the server then only accepts
  connections from the Mac itself.
- **Cloud AI**: remove `ALLOW_CLOUD_AI` (default is blocked).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Header says "AI: off" | Start Ollama (`ollama serve`) and pull the model in `OLLAMA_MODEL`. The Privacy tab shows the exact reason. |
| "Quoted from sources · no AI" answers | Same as above. The model is unreachable, so the assistant quotes passages instead of guessing. |
| Scanned PDF shows "could not be read" | `brew install tesseract`, restart, **Re-scan**. |
| Wrong day/month on invoices | Set `DATE_ORDER=DMY` (ambiguous dates are always flagged for review either way). |
| Answers are slow | Use a smaller model (e.g. `qwen2.5:14b` instead of 32b), or lower `PREFETCH_PASSAGES`. |
| Start over for a demo | `make demo` (deletes `storage/` and uploads; keeps the sample data). |
| Server log | Terminal output, or `storage/server.log` when started by launchd. |

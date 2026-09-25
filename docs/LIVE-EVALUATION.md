# Live end-to-end check

`scripts/evaluate_live.py` tests a **running** install over HTTP, as a user would reach it. It complements
[TEST-RESULTS.md](TEST-RESULTS.md) (offline accuracy) and the automated tests.

## Latest run in the development environment (2026-09-24)

Setup: the app with a **3B model (Qwen 2.5 3B Instruct, GGUF) on a shared CPU** through a local
OpenAI-compatible server, semantic search with `nomic-embed-text`, the full sample corpus. This is far
smaller and slower than the Mac Studio target (14B+ model on the Apple GPU), so treat quality and
latency here as a floor, not a forecast.

| Check | Result |
|---|---|
| Documents loaded | 16 documents + 3 tables (incl. the 4 real public documents) |
| Originals downloadable | 16 / 16 |
| Invoices extracted / flagged for review | 8 / 5 |
| Invoice lab flags a malformed invoice | yes |
| Dashboard | yes |
| Project report flags the document-vs-spreadsheet discrepancy | yes |
| Answer cites the lease for a lease question | 2 / 2 |
| Answer from the 28-page IRS publication, with page ("at least 4 years", p.16) | 1 / 1 |
| "Not found" for a question the documents can't answer | 1 / 1 |
| Prompt injection refused | 1 / 1 |
| Accounting question answered from the data | 1 / 1 |
| Table question over the country-codes CSV (needs SQL) | 0 / 1: the 3B model searched documents instead of querying the table |
| Latency (warm, CPU shared with a test run) | p50 ≈ 139 s, p95 ≈ 158 s; not representative of the Mac Studio |

Issues this check found, all fixed and covered by tests:

- a 3B model sometimes writes its tool call as text (`<tool_call>{…}</tool_call>`); that text was shown
  as the answer. Such calls are now executed and leftover markup is stripped;
- "How long should a business keep … records?" was routed as an accounting question without document
  search; retention questions now route to documents, and accounting questions also get passages;
- the 56-column country-codes table was cut off at 40 columns in what the model sees, hiding the country
  name columns; every column is now listed, quoted the way DuckDB needs.

## Run it yourself

```bash
python scripts/evaluate_live.py --base-url http://127.0.0.1:8000 --runs 20 --output evaluation.json
python scripts/evaluate_live.py --base-url https://your-domain/rag-assistant/ --pace-seconds 20   # hosted demo
```

Add `--max-p95-ms 5000` (or your target) to make latency a pass/fail criterion on the hardware you are
accepting. Table questions are only attempted when a model is connected.

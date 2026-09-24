# Security and privacy

## Principles

1. **Local by default.** Documents, the search index, the database, OCR and the AI model all run on
   the Mac. With default settings **no data leaves the machine**.
2. **Approval before anything external.** Cloud AI is blocked unless `ALLOW_CLOUD_AI=true` is set by
   the owner. Emails, accounting changes and other external actions can only be *proposed*; a named
   person approves them, and in this stage they're still not executed.
3. **Never invent.** Missing or uncertain information is flagged, not filled in. AI-extracted values
   are only accepted if they appear verbatim on the document.
4. **Minimum access.** QuickBooks is read-only in code; the SQL layer is read-only with no file or
   network access; only allowlisted tables can be queried.
5. **Traceable.** Every significant action is written to a tamper-evident log.

## Data flows

| Flow | Where it goes | When |
|---|---|---|
| Documents, invoices, spreadsheets | `DATA_DIR` on the Mac → parsed locally → `storage/` | always |
| Scanned pages / photos | Tesseract OCR on the Mac | always |
| Questions + retrieved passages | the local model (Ollama) | when a model is running |
| Questions + passages + query results | the cloud model provider | **only** if `ALLOW_CLOUD_AI=true`, a key is configured, **and** the request's data classes are in `CLOUD_ALLOWED_DATA` (default: documents only), with emails/phones/account numbers masked. Accounting, invoice and bank data and high-risk identifiers go to local models only. |
| QuickBooks queries | Intuit API (read-only GET) | only in `QBO_MODE=sandbox` |
| Email | not connected in this prototype | — |

The **Privacy & security** tab shows this live for the current configuration.

## Controls

| Area | Control |
|---|---|
| Network exposure | Binds to `127.0.0.1` by default; optional `APP_API_KEY` for any other client; rate limiting; request-size limits; security headers (`nosniff`, `DENY` framing, `no-referrer`, `no-store`). |
| AI routing | Privacy router decides local vs cloud per request from data classes and PII; the tool layer blocks cloud models from reading non-allowed tables/documents; earlier local-only turns are withheld from cloud models; every answer records which model handled it (see [LLM-ROUTING.md](LLM-ROUTING.md)). |
| Type safety | Model tool calls and structured outputs are validated against Pydantic schemas before use; invalid output is rejected and retried, never executed. |
| API keys | Provider keys live only in `.env` (git-ignored), are never sent to the browser, and are redacted by the output scrubber if a model ever echoes one. |
| Prompt injection | Input firewall (instruction override, role-play jailbreaks, secret fishing, unsafe SQL, format hijacking); a system prompt that treats documents and tool output as data; output scrubber that redacts credential-shaped strings. |
| SQL | DuckDB with external access disabled (no file/network reads); the query is parsed and every table checked against an allowlist; single `SELECT` only; hard row cap. |
| QuickBooks | GET-only client; `SELECT * FROM <allowlisted entity>` only; single-use OAuth `state` checked; production refused by default; one-click revoke + local data deletion. |
| Credentials | QuickBooks tokens in a `0600` file under `storage/secrets/` or the macOS Keychain (`SECRETS_BACKEND=keyring`). `.env` and `storage/` are git-ignored. The app never asks for banking credentials or company passwords. |
| Files | Uploads are type-checked and size-limited and stored under `DATA_DIR/uploads/`; file names are sanitised. Original-file links are confined to `DATA_DIR` (path traversal blocked). Word files are read as XML; macros are never executed. |
| Audit | `storage/audit.jsonl`: each entry holds the SHA-256 of the previous one, so an edited or deleted line is detected (**Activity log** shows "Integrity verified"). |
| Human review | Every invoice extraction starts as *needs review*; approvals and rejections require a name; reviews are logged. |
| Encryption at rest | Use FileVault on the Mac (covers documents, database, log and tokens). |
| Model training | Local models don't learn from use. Cloud AI is off by default; if approved, use a provider tier that contractually excludes training and retention. |

## Identity in this prototype

The name typed in the UI is used to attribute reviews and approvals in the log. It is **not**
authentication. This prototype is intended for a single trusted Mac. Per-user sign-in (e.g. Microsoft
365 / Google SSO) with roles such as *viewer* / *reviewer* / *approver* is planned for the pilot
stage (see [ROADMAP.md](ROADMAP.md)).

## Handling real data (later stage)

- Start with a small, non-confidential document set; confirm results before widening access.
- Keep `DATA_DIR` and `storage/` on the encrypted internal disk, not in a synced cloud folder.
- Company data is not copied off the Mac or retained by the developer without written permission.

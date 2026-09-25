# Known limitations and proposed next stages

## Known limitations of this prototype

**Data and accuracy**

- The results in [TEST-RESULTS.md](TEST-RESULTS.md) come from a **small, clean, synthetic** set (13
  documents, 11 invoices). Real invoices vary more: multi-page invoices, handwritten notes, poor scans,
  foreign formats. Expect lower accuracy until the extractor is tuned on the company's actual
  suppliers. The confidence scores and review step exist for this reason.
- Invoice extraction reads header fields (supplier, number, dates, PO, subtotal, tax, total,
  currency) and line items from simple single-line tables (description, qty, unit price, amount), with
  sum checks. Wrapped descriptions and unusual table layouts may be missed; totals are still checked.
- Multi-page invoices are read as one text; totals printed only on a later page are found, but table
  structure across pages isn't reconstructed.
- Ambiguous numeric dates are read using `DATE_ORDER` (default month/day/year) and always flagged.
- Search combines keywords with a local semantic embedding model (`nomic-embed-text`) when Ollama has
  it, and falls back to an offline hashing embedder otherwise (weaker on paraphrases). On the sample set
  semantic search found 7/8 reworded questions (6/8 after the 28-page IRS publication was added);
  a larger, real document set should be measured again.
- Bank matching uses amounts, dates and references/names on a single statement; split payments,
  batched payments and multi-currency accounts are not matched yet.
- Reports are drafts built from the loaded data and flagged as such; they are not a substitute for the
  accountant's review.
- The Docker image and CI pipeline are defined, but the image was built in CI, not verified on the Mac
  Studio itself; on macOS the recommended setup is the native install (Ollama needs the Apple GPU).
- The hosted public demo (Vercel + Cloudflare Worker) is configured and tested locally (including under a
  path prefix through a proxy that mimics the Worker) but has not been deployed or measured from here.
- A 3B model on CPU often skips SQL for table questions (e.g. the country-codes CSV) or writes tool calls as
  text; text-written calls are now executed, but table answers need the recommended 14B+ model.
- Streaming, semantic search and model-assisted features were verified with a small (3B) model on CPU;
  answer quality and speed must be re-measured on the Mac Studio with the chosen model.
- Spreadsheets load the first sheet of an XLSX only.

**AI model**

- Answer quality depends on the local model. Qwen 2.5 14B or larger with native tool calling is
  recommended. In this repository's cloud test environment only a 3B model on CPU was available: it
  answered document questions correctly with citations and declined unanswerable ones, but did not
  reliably issue SQL queries on its own. Table questions need the recommended larger model.
  **End-to-end AI answers should be re-verified on the Mac Studio** (`scripts/evaluate.py --with-ai`
  plus the demo questions).
- The cloud provider integrations (Claude, OpenAI, Gemini, OpenRouter, Azure, Groq, Mistral, DeepSeek,
  Together, xAI) are implemented against their documented APIs and tested with recorded/fake HTTP
  responses. They have not yet been run against live accounts. Do a smoke test with each key you
  add (ask a document question and check the **AI models** tab). Default model names are placeholders
  to be replaced with the exact models on your account.
- Data-class and PII detection is rule-based (folders, table names, patterns). It errs on the side of
  keeping data local, but it isn't a full DLP system; review `CLOUD_ALLOWED_DATA` and
  `SENSITIVE_PATHS` with the owner before enabling cloud AI on real data.
- Without a model the assistant still works (extraction, reconciliation, quoted passages) but doesn't
  compose answers.

**Security and operations**

- Single-machine, single-trust-zone prototype: the reviewer name is not authentication. There are no
  user accounts or roles yet.
- QuickBooks: tested against the bundled fixture and mocked Intuit endpoints; the live sandbox OAuth
  flow needs a developer app's keys to exercise.
- Email is not connected (not required at this stage).
- Approved actions are recorded but not executed (by design for this stage).

## Proposed stages

Durations are indicative and depend on document volumes, hardware and access, which are still to be
confirmed.

### Stage 1: Prototype on the Mac Studio (this repository), ~1–2 weeks

- Install on the agreed hardware; select the model for its memory size; enable FileVault.
- Run on an agreed, non-confidential sample of real documents and invoices (plus unseen ones);
  tune extraction for the company's main suppliers; publish updated test results.
- Connect the Intuit **sandbox** company (read-only) and demonstrate reconciliation.
- Deliverables: working install, source and configuration under the company's control, install /
  operations docs, dependency and licence list, test results.

### Stage 2: Pilot, ~3–5 weeks

- User sign-in (Microsoft 365 or Google) with roles: viewer, reviewer, approver.
- Multi-page invoice tables; bank feeds straight from the bank or QuickBooks; split/batched payment
  matching; scheduled reports.
- **Read-only** email search and summaries (Microsoft Graph or Gmail API with minimum scopes);
  drafted replies go to the Approvals queue, never sent automatically.
- QuickBooks **production, read-only**, after Intuit app approval; scheduled daily sync.
- Encrypted backups; alerting on failed syncs; activity-log export.

### Stage 3: Controlled actions, ~3–4 weeks, only if approved

- Execute approved actions: record approved bills in QuickBooks, send approved emails. Each one
  individually approved, logged, and reversible where possible.
- Reports and document drafting (project summaries, outstanding-task lists) on a schedule.
- Hardening review, handover, and a support/maintenance arrangement.

## Dependencies and risks

| Risk | Mitigation |
|---|---|
| Mac Studio memory limits model size | Model table in INSTALL-MAC.md; test on the actual hardware in stage 1 |
| Real invoices less regular than samples | Per-field confidence, mandatory review, tuning on real suppliers, unseen-document tests |
| Intuit production approval / API terms | Start in sandbox; apply for production early in stage 2; read-only volume is low |
| Scope creep into external actions | Approval queue is the only path; execution is a separate, explicit stage |
| Sensitive data handling | Synthetic data until approved; local-only by default; FileVault; no cloud AI without sign-off |

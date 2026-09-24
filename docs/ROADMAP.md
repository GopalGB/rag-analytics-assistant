# Known limitations and proposed next stages

## Known limitations of this prototype

**Data and accuracy**

- The results in [TEST-RESULTS.md](TEST-RESULTS.md) come from a **small, clean, synthetic** set (13
  documents, 11 invoices). Real invoices vary more: multi-page invoices, handwritten notes, poor scans,
  foreign formats. Expect lower accuracy until the extractor is tuned on the company's actual
  suppliers. The confidence scores and review step exist for this reason.
- Invoice extraction reads header fields (supplier, number, dates, PO, subtotal, tax, total,
  currency). Line items are not extracted into a table yet.
- Multi-page invoices are read as one text; totals printed only on a later page are found, but table
  structure across pages isn't reconstructed.
- Ambiguous numeric dates are read using `DATE_ORDER` (default month/day/year) and always flagged.
- Search uses keyword + an offline hashing embedder. That's strong for names, numbers and terms, weaker
  for paraphrases ("end of the tenancy" vs "expiry"). A local semantic embedding model is a planned
  upgrade.
- Spreadsheets load the first sheet of an XLSX only.

**AI model**

- Answer quality depends on the local model. Qwen 2.5 14B or larger with native tool calling is
  recommended. In this repository's cloud test environment only a 3B model on CPU was available: it
  answered document questions correctly with citations and declined unanswerable ones, but did not
  reliably issue SQL queries on its own. Table questions need the recommended larger model.
  **End-to-end AI answers should be re-verified on the Mac Studio** (`scripts/evaluate.py --with-ai`
  plus the demo questions).
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
- Semantic local embeddings; line-item extraction; multi-page invoice tables; bank-statement import
  and matching of payments to bills.
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

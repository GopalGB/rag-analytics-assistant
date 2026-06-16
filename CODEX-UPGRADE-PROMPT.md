# Codex Upgrade Prompt

Paste the block below into Codex **with your internal project open**. It points Codex at this public
reference repo, has it absorb the architecture and the adoption guide, and apply each improvement to
**your** codebase — mapping the generic names onto your real files. Your code and data never leave your
machine; only the generic reference flows in.

---

```
You are upgrading THIS internal codebase (currently open in your workspace).

Reference architecture (generic, public, synthetic data only):
  https://github.com/GopalGB/rag-analytics-assistant

STEP 1 — ABSORB
- Fetch / read that repo, especially: README.md, ADOPTION-GUIDE.md, and everything under app/.
- Understand its LLM-first design: a bounded tool-calling loop (run_sql + search_docs) as the primary
  brain — strictly LLM-only (no deterministic answerer; it fails fast if no model is configured),
  pluggable model backends (OpenAI / AWS Bedrock / local CLI behind one interface), auto-ingest that
  re-embeds + re-indexes new files in the data dir on the fly, a safe DuckDB SELECT layer, hybrid
  BM25+vector retrieval, per-session memory, an input firewall, and an output scrubber.

STEP 2 — APPLY (work top-to-bottom through ADOPTION-GUIDE.md)
For each numbered item in ADOPTION-GUIDE.md:
  a. Find the equivalent file/area in OUR codebase.
  b. Apply the same pattern, mapping the reference's generic names (sales, revenue, region, run_sql)
     onto OUR real table/column/file names.
  c. Make the change as a small, reviewable edit. Show the diff.
  d. Run OUR test suite. If it goes red, fix before moving on. Never proceed on a failing suite.

HARD RULES
- Do NOT copy the reference's synthetic data. Keep OUR data and configuration in place.
- Do NOT remove or weaken OUR security layer. If the reference has a guard rule we lack, ADD it.
- Do NOT publish, push, or exfiltrate any of OUR code or data anywhere. All work stays local.
- Preserve existing behavior unless an item explicitly changes it, and only with passing tests.
- If our model id is a placeholder or a guess, replace it with a real model we have access to —
  do not invent one.

PRIORITY ORDER (highest impact first)
1. Strictly LLM-only tool-calling engine — remove any hand-rolled answer path (items 1, 2).
2. Conversation memory (item 3).
3. Atomic table reload to stop data loss (item 5).
4. Pluggable model backends + auto-ingest of new data (items 13, 14).
5. Remove dead/broken modules; keep imports honest (item 6).
6. Everything else in the guide.

STEP 3 — REPORT
Produce a summary: for each guide item — DONE / SKIPPED (why), the files changed, and the test result.
End with any item that needs a human decision.
```

---

After Codex finishes, run your own tests once more and review the diff before committing **inside your
internal repo**. Nothing here goes to a public location.

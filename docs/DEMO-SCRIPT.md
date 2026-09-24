# Demo script (about 12 minutes)

Start from a clean state: `make demo` (clears previous reviews, approvals, log and uploads, then starts
the server). Open <http://127.0.0.1:8000> and type a name in **You** (top right) — reviews and approvals
are recorded against it.

## 0. The overview (1 min)

**Overview** tab (opens first): money owed and owing, cash, overdue items, then charts: payables and
receivables by age, spend by supplier, cash flow from the bank statement, and budget vs. actual with the
car park resurfacing line over budget. Hover or tab onto a bar for its value; **Table** shows the same
numbers as text. Below, the bank statement is matched line by line to QuickBooks: Harbor Waste's bill
is marked *paid* in QuickBooks, but no payment appears on the statement.

## 1. Privacy first (1 min)

**Privacy & security** tab: every function, where it runs, whether it needs the internet, and what
leaves the machine. With the defaults the answer is "nothing". Cloud AI is blocked until approved.

## 2. Ask questions with sources (3 min)

**Ask** tab. Click the example questions or type:

- *When does the office lease expire and what notice is needed to renew?* → 30 June 2029, notice by
  31 Dec 2028, citing `Office_Lease_Summary.docx`.
- *What is the monthly fee in the Coastal Plumbing maintenance agreement?* → $2,310.00, citing the PDF
  page. Click the source to open the original at that page.
- *What tasks are outstanding on the Riverside renovation project?*
- *Who is our auditor?* → "I couldn't find this": it doesn't guess.
- With a local model running, also try: *Which customers have overdue balances?* (queries the
  QuickBooks tables) and *Draft an email to Oakridge Accounting Partners about their overdue
  invoices* (the draft appears in **Approvals** and is not sent).

With a model, answers stream in word by word and show which route, model and tools were used; a
question like *Show spend by supplier* also gets a chart.

Real public documents are in the corpus too: *How long should a business keep employment tax
records?* answers from the 28-page IRS publication with the page cited ("at least 4 years").

Without an AI model the assistant quotes the most relevant passages (labelled "Quoted from sources ·
no AI") instead of writing an answer.

## 3. Invoices, including a scan (2 min)

**Invoices** tab: 8 supplier invoices read automatically. Open:

- `pioneer_PCW-0192_scanned.pdf`: an image-only scan, read with local OCR and flagged "check against
  the original".
- `greenleaf_GL-2291.pdf`: date `05/12/2026` flagged as ambiguous (May 12 or 5 December?).
- `harbor_waste_no_number.pdf`: invoice number **missing**. It is left empty, not invented. Type
  a number and **Approve**; the correction is recorded.
- `brightspark_BSC-118.pdf`: subtotal + tax ≠ total. Flagged.
- `summit_INV-10421_resubmitted.pdf`: flagged as a duplicate of an invoice already on file.

Each field shows a confidence bar and the exact line it was read from.

**Invoice lab** (bottom of the Invoices tab): click **Try a flawed example**. The pasted invoice says
3 × $10.00 but a total of $25.00: the mismatch is flagged and each field shows the line it came from.
Paste any other invoice text to show it reads new layouts; nothing is stored.

## 4. A document it has never seen (1 min)

**Documents** tab → drop `data/unseen_invoices/lakeside_catering_LC-3390.png` (a phone-photo invoice)
and `northgate_NSS-2026-044.pdf` (a new layout). Both are OCR'd or parsed, classified as invoices and
extracted within seconds.

## 5. QuickBooks, read-only (2 min)

**QuickBooks** tab: the sandbox company is synced read-only. **Invoices vs QuickBooks** shows:

- **amount mismatch**: Coastal Plumbing 5530 is $2,310.00 on the invoice (and in the contract) but
  $2,130.00 in QuickBooks. Looks like a keying error. **Propose: query supplier** drafts an email for
  approval.
- **not in QuickBooks**: Metro Office and BrightSpark invoices haven't been recorded. **Propose: record
  bill** queues it for approval.
- **no document**: bills in QuickBooks with no supporting invoice on file.
- **matched**: the uploaded Northgate and Lakeside invoices now match their bills.

## 5b. Draft reports (1 min)

**Reports** tab: accounts summary, aging, outstanding items and project status, built from the data with
sources. The project report quotes the status PDF and flags that it states $118,650.00 spent while the
budget spreadsheet totals $88,590.00: two documents that disagree, surfaced rather than smoothed over.
Download as Markdown, open the printable page, or click **AI summary** (runs on the local model only,
because it contains accounting data).

## 6. Approvals and the audit trail (1 min)

**Approvals**: approve one proposal and reject another. Approved actions are logged and **not
executed**, because this stage is read-only.

**Activity log**: every question, upload, review, sync and approval with who and when. The
"Integrity verified" badge comes from the hash chain; editing any past line breaks it.

## Talking points

- Runs on the Mac Studio; models, documents and processing stay local.
- Never invents: missing or uncertain values are flagged for a person.
- Accounting outputs are drafts; external actions need a named approver.
- QuickBooks access is read-only in code, and can be revoked with one click.

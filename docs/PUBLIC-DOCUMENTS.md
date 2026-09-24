# Real public documents in the sample corpus

Besides the synthetic company files, the sample data includes four **real, unmodified documents from
official publishers**, each a different file type, so every reader path is exercised with real-world
layouts (a 28-page PDF, a Word file with tables, long Markdown, a wide CSV). Retrieved 2026-09-24.

| File | Type | Title | Publisher | Source | Licence | Bytes |
|---|---|---|---|---|---|---|
| `documents/public/public_irs_business_records.pdf` | PDF, 28 pages | Publication 583, Starting a Business and Keeping Records (Rev. Dec 2024) | US Internal Revenue Service | https://www.irs.gov/pub/irs-pdf/p583.pdf | Public domain (US federal government work, 17 U.S.C. 105) | 1,365,106 |
| `documents/public/public_govuk_sample_invoice_installers.docx` | DOCX | Sample invoice for installers | UK Government (GOV.UK) | https://assets.publishing.service.gov.uk/media/652512baaea2d0000d219a72/Sample_invoice_for_installers_.docx | Open Government Licence v3.0 | 46,802 |
| `documents/public/public_owasp_llm01_prompt_injection.md` | Markdown | LLM01:2025 Prompt Injection (OWASP Top 10 for LLM Applications) | OWASP Foundation | https://github.com/OWASP/www-project-top-10-for-large-language-model-applications/blob/main/2_0_vulns/LLM01_PromptInjection.md | CC BY-SA 4.0 | 10,555 |
| `tables/public_country_codes.csv` | CSV, 249 rows × 56 columns | Country codes (ISO 3166, ISO 4217 currency, dialling codes) | Frictionless Data / DataHub | https://github.com/datasets/country-codes | ODC PDDL / CC0 (public domain) | 134,003 |

Paths are relative to `data/sample/`.

## Questions to try

- IRS PDF: *How long should a business keep its employment tax records?* · *What records support business expenses?*
- GOV.UK DOCX: *What must an invoice include for a grant claim to be accepted?*
- OWASP Markdown: *What is the difference between direct and indirect prompt injection?* · *How can prompt injection be mitigated?*
- CSV (SQL, needs an AI model): *What currency and dialling code does the United Arab Emirates use?* (expected: AED, +971)

## Notes

- The IRS PDF's first page carries a printer-proof header ("Draft Ok to Print"). That is part of the
  original file, not a data error.
- The GOV.UK file is invoice *guidance*, not an invoice. It sits under `documents/`, so it is indexed as a
  document; pasted into the Invoice lab, every key field is reported as missing — a good demonstration
  of flagging instead of inventing.
- The OWASP page contains example prompt-injection text. It is indexed as ordinary content: retrieved
  passages are treated as data, never as instructions, which makes it a useful live test of that rule.
- OWASP content is CC BY-SA 4.0: keep this attribution when redistributing it.

## More invoice layouts

`data/unseen_invoices/more_layouts/` holds 11 extra synthetic invoices in simple layouts (plain text and
one-page PDFs, USD and AED): five complete ones, three complete PDFs with subtotal + tax, and three
deliberately flawed ones (`Total: MISSING`, a quantity of "two" and a "negotiable" price, and
3 × $10.00 against a $25.00 total). Upload them in the demo, or paste one into the Invoice lab; the tests
check every expected value and flag.

# Real public documents in the demo corpus

Besides the synthetic FLC-style files, the demo includes four real documents from official publishers.
Each one is a different file type, so the demo exercises every reader path (PDF, DOCX, Markdown, CSV).
Files are unmodified originals, retrieved 2026-09-24.

| File | Type | Title | Publisher | Source | Licence | Bytes |
|---|---|---|---|---|---|---|
| `public_irs_business_records.pdf` | PDF, 28 pages | Publication 583, Starting a Business and Keeping Records (Rev. Dec 2024) | US Internal Revenue Service | https://www.irs.gov/pub/irs-pdf/p583.pdf | Public domain, US federal government work (17 U.S.C. 105) | 1,365,106 |
| `public_govuk_sample_invoice_installers.docx` | DOCX | Sample invoice for installers | UK Government (GOV.UK) | https://assets.publishing.service.gov.uk/media/652512baaea2d0000d219a72/Sample_invoice_for_installers_.docx | Open Government Licence v3.0 | 46,802 |
| `public_owasp_llm01_prompt_injection.md` | Markdown | LLM01:2025 Prompt Injection (OWASP Top 10 for LLM Applications) | OWASP Foundation | https://github.com/OWASP/www-project-top-10-for-large-language-model-applications/blob/main/2_0_vulns/LLM01_PromptInjection.md | CC BY-SA 4.0 | 10,555 |
| `public_country_codes.csv` | CSV, 249 rows x 56 columns | Country codes (ISO 3166, ISO 4217 currency, dialling codes) | Frictionless Data / DataHub | https://github.com/datasets/country-codes | ODC PDDL / CC0 (public domain) | 134,003 |

## Demo questions

- IRS PDF: "How long should a business keep its employment tax records?" and "What records support expenses?"
- GOV.UK DOCX: "What must an invoice include for a grant claim to be accepted?"
- OWASP Markdown: "What is the difference between direct and indirect prompt injection?" and "How can prompt injection be mitigated?"
- CSV (SQL): "What currency and dialling code does the United Arab Emirates use?" (expected answer: AED, +971)

## Notes

- The IRS PDF's first page carries a printer-proof header ("Draft Ok to Print"). That is part of the original file, not a data error.
- The GOV.UK DOCX is invoice guidance, not an invoice, so the invoice extractor correctly returns every field as missing. This is a good demo of the "flag missing information" requirement.
- OWASP content is CC BY-SA 4.0: keep this attribution when redistributing it.

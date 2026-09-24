import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function helpers() {
  const html = await readFile(new URL("../app/ui/chat.html", import.meta.url), "utf8");
  const start = html.indexOf("/* UI_HELPERS_START */");
  const end = html.indexOf("/* UI_HELPERS_END */");
  assert.notEqual(start, -1, "UI helper exports are missing");
  assert.notEqual(end, -1, "UI helper exports are missing");
  return Function(`${html.slice(start, end)};return {escapeHtml,safeDocumentUrl,errorMessage,healthCounts,reviewFields,invoiceWarnings}`)();
}

test("UI helpers escape API text and resolve only local document links", async () => {
  const { escapeHtml, safeDocumentUrl } = await helpers();
  assert.equal(escapeHtml('<img src=x onerror="alert(1)">'), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
  assert.equal(safeDocumentUrl("/rag-assistant", "/documents/invoice 1.pdf"), "/rag-assistant/documents/invoice%201.pdf");
  assert.equal(safeDocumentUrl("/rag-assistant", "/rag-assistant/documents/invoice.pdf"), "/rag-assistant/documents/invoice.pdf");
  for (const value of ["javascript:alert(1)", "https://evil.example/doc", "//evil.example/doc", "/documents/../secret", "/documents/%2e%2e/secret"]) {
    assert.equal(safeDocumentUrl("/rag-assistant", value), "");
  }
});

test("UI helpers keep loaded counts and expose bounded HTTP errors", async () => {
  const { errorMessage, healthCounts } = await helpers();
  assert.deepEqual(healthCounts({ documents: 2, chunks: 4 }, { documents: 9, chunks: 20 }), { documents: 2, chunks: 4 });
  assert.deepEqual(healthCounts(null, { documents: 9, chunks: 20 }), { documents: 9, chunks: 20 });
  assert.equal(errorMessage(429, { detail: "<b>slow down</b>" }), "Request failed (429): slow down");
  assert.equal(errorMessage(500, { detail: "secret stack trace" }), "The server could not complete the request.");
});

test("UI helper follows the invoice review_fields API contract", async () => {
  const { reviewFields } = await helpers();
  const fixture = { missing_fields: ["amount"], review_fields: ["amount_mismatch", "ambiguous_date"] };
  assert.deepEqual(reviewFields(fixture), ["amount_mismatch", "ambiguous_date"]);
  assert.deepEqual(reviewFields({ missing_fields: ["amount"] }), []);
});

test("invoice warnings keep missing and review flags visible", async () => {
  const { invoiceWarnings } = await helpers();
  const flagged = invoiceWarnings({ missing_fields: ["amount"], review_fields: ["amount_mismatch"] });
  assert.deepEqual(flagged, { missing: ["amount"], review: ["amount_mismatch"], complete: false });
  assert.equal(invoiceWarnings({ missing_fields: [], review_fields: [] }).complete, true);
  assert.equal(invoiceWarnings({}).complete, true);
});

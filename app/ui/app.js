/* Private AI Assistant — UI logic. Loaded as a file (not inline) so the app can use a strict CSP. */
const $ = (s) => document.querySelector(s);
const sessionId = (() => { try { let id = localStorage.getItem("session_id"); if (!id) { id = "s-" + Math.random().toString(36).slice(2); localStorage.setItem("session_id", id); } return id; } catch { return "s-" + Math.random().toString(36).slice(2); } })();
const who = $("#who");
try { who.value = localStorage.getItem("who") || ""; } catch {}
who.addEventListener("change", () => { try { localStorage.setItem("who", who.value.trim()); } catch {} });

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v); else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) e.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return e;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 3200); }
function headers(json = true) { const h = { "X-User": encodeURIComponent(who.value.trim()) }; if (json) h["Content-Type"] = "application/json"; return h; }
async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { ...headers(!(opts.body instanceof FormData)), ...(opts.headers || {}) } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || body.error || r.statusText);
  return body;
}
const money = (v) => v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fileName = (f) => (f || "").split("/").pop();
function fileLink(file, page, label) {
  const href = "/files/" + file.split("/").map(encodeURIComponent).join("/") + (page ? "#page=" + page : "");
  return el("a", { href, target: "_blank", rel: "noopener", text: label || fileName(file) });
}
function confBar(c) {
  const pct = Math.round((c || 0) * 100);
  const color = c >= 0.85 ? "var(--ok)" : c >= 0.6 ? "var(--warn)" : "var(--bad)";
  const bar = el("span", { class: "bar" }, el("i", { style: `width:${pct}%;background:${color}` }));
  return el("span", { class: "conf" }, bar, el("span", { class: "small muted", text: pct + "%" }));
}
function reviewer() {
  const name = who.value.trim();
  if (!name) { toast("Enter your name (top right) first — reviews and approvals are recorded against it."); who.focus(); }
  return name;
}
function table(tbl, cols, rows, onClick, selId) {
  tbl.replaceChildren();
  tbl.append(el("tr", {}, cols.map((c) => el("th", { class: c.num ? "num" : "", text: c.label }))));
  if (!rows.length) { tbl.append(el("tr", {}, el("td", { colspan: cols.length, class: "muted", text: "Nothing here yet." }))); return; }
  for (const r of rows) {
    const tr = el("tr", { class: (onClick ? "click " : "") + (selId && r.id === selId ? "sel" : "") }, cols.map((c) => {
      const v = c.render ? c.render(r) : r[c.key];
      return el("td", { class: c.num ? "num" : "" }, v instanceof Node ? v : (v === null || v === undefined ? "—" : String(v)));
    }));
    if (onClick) tr.addEventListener("click", () => onClick(r));
    tbl.append(tr);
  }
}

// ---------------- tabs
const loaders = {};
function show(tab) {
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll("main section").forEach((s) => s.classList.toggle("active", s.id === "tab-" + tab));
  if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
  loaders[tab] && loaders[tab]();
}
document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.tab)));
window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (document.getElementById("tab-" + t)) show(t); });

// ---------------- header status
async function refreshHealth() {
  try {
    const h = await api("/health");
    $("#app-name").textContent = h.app_name;
    const llm = $("#chip-llm");
    llm.textContent = h.llm_enabled ? `AI: ${h.llm} (${h.llm_local ? "local" : "cloud"})${h.models > 1 ? ` +${h.models - 1} more` : ""}` : "AI: off · quoting sources only";
    llm.style.cursor = "pointer"; llm.onclick = () => show("models");
    llm.className = "chip " + (h.llm_enabled ? (h.llm_local ? "ok" : "warn") : "warn");
    llm.title = h.llm_note || "";
    const ocr = $("#chip-ocr"); ocr.textContent = "OCR: " + h.ocr; ocr.className = "chip " + (h.ocr.startsWith("tesseract") ? "ok" : "warn");
    const q = $("#chip-qbo"); const qb = h.qbo;
    q.textContent = `QuickBooks: ${qb.mode === "mock" ? "test company (offline)" : qb.mode}${qb.connected ? "" : " · not connected"}`;
    q.className = "chip " + (qb.connected ? "ok" : "warn");
    $("#chip-docs").textContent = `${h.documents} docs · ${h.invoices} invoices`;
    const ap = $("#n-approvals"); ap.hidden = !h.pending_approvals; ap.textContent = h.pending_approvals;
  } catch { $("#chip-llm").textContent = "server unreachable"; }
}

// ---------------- ASK
function renderAnswer(text) {
  // Escape-first, then highlight [citations] and **bold**. Never injects raw HTML.
  const frag = document.createDocumentFragment();
  const parts = String(text).split(/(\[[^\]\n]{2,120}\]|\*\*[^*\n]+\*\*)/g);
  for (const p of parts) {
    if (/^\[[^\]]+\]$/.test(p)) frag.append(el("span", { class: "cite", text: p.slice(1, -1) }));
    else if (/^\*\*[^*]+\*\*$/.test(p)) frag.append(el("strong", { text: p.slice(2, -2) }));
    else frag.append(p);
  }
  return frag;
}
function addMsg(role, text, payload) {
  const bubble = el("div", { class: "bubble" });
  if (role === "user") bubble.textContent = text; else bubble.append(renderAnswer(text));
  const wrap = el("div", { class: "msg " + role }, bubble);
  if (payload) {
    const routeLabel = { agent: ["AI answer", "ok"], extractive: ["Quoted from sources · no AI", "warn"], refused: ["Refused", "bad"], greeting: ["", ""] }[payload.route] || [payload.route, "info"];
    const meta = el("div", { class: "meta" });
    if (routeLabel[0]) meta.append(el("span", { class: "pill " + routeLabel[1], text: routeLabel[0] }));
    const rt = payload.routing;
    if (rt) {
      if (rt.intent) meta.append(el("span", { class: "pill info", title: `routed by ${rt.method}: ${rt.reason || ""}`, text: rt.intent }));
      if (rt.model) meta.append(el("span", { class: "pill " + (rt.model_local ? "ok" : "warn"), text: `${rt.model} · ${rt.model_local ? "local" : "cloud"}` }));
      const pv = rt.privacy || {};
      if (pv.local_only && pv.reasons && pv.reasons.length) meta.append(el("span", { class: "pill ok", title: pv.reasons.join("; "), text: "kept on this machine" }));
      if (pv.redactions) meta.append(el("span", { class: "pill info", text: `${pv.redactions} detail(s) masked for cloud` }));
      if (pv.withheld_passages) meta.append(el("span", { class: "pill info", text: `${pv.withheld_passages} sensitive passage(s) withheld from cloud` }));
      if (rt.fallbacks) meta.append(el("span", { class: "pill warn", text: `${rt.fallbacks} fallback(s)` }));
    }
    if (payload.checks && payload.checks.unverified_citations && payload.checks.unverified_citations.length)
      meta.append(el("span", { class: "pill bad", title: payload.checks.unverified_citations.join(", "), text: "citation not in retrieved sources" }));
    if (meta.childNodes.length) wrap.append(meta);
    if (rt && rt.attempts && rt.attempts.length) {
      const d = el("details", {}, el("summary", { text: "How this was answered" }));
      const lines = [`Task: ${rt.intent || "-"} (${rt.method || "-"}) · tier: ${rt.tier || "-"} · tools: ${(rt.tools || []).join(", ") || "-"}`];
      if (rt.privacy && rt.privacy.reasons && rt.privacy.reasons.length) lines.push("Privacy: " + rt.privacy.reasons.join("; "));
      rt.attempts.forEach((a, i) => lines.push(`${i + 1}. ${a.model} (${a.local ? "local" : "cloud"}) — ${a.ok ? "answered" : "failed: " + a.error} · ${a.ms} ms · ${a.input_tokens}+${a.output_tokens} tokens${a.cost_usd ? " · $" + a.cost_usd.toFixed(4) : ""}`));
      (rt.tool_calls || []).forEach((t) => lines.push(`tool ${t.tool}: ${t.ok ? "ok" : t.error}`));
      d.append(el("pre", { text: lines.join("\n") }));
      wrap.append(d);
    }
    if (payload.sources && payload.sources.length) {
      const box = el("div", { class: "sources" });
      payload.sources.slice(0, 6).forEach((s) => box.append(el("div", { class: "source" },
        fileLink(s.file, s.page, s.cite), el("span", { class: "muted small", text: "  " + s.file }),
        el("div", { class: "snip", text: (s.snippet || "").slice(0, 260) + "…" }))));
      wrap.append(el("details", { open: "" }, el("summary", { text: `Sources (${payload.sources.length})` }), box));
    }
    if (payload.actions && payload.actions.length) {
      payload.actions.forEach((a) => wrap.append(el("div", { class: "note", style: "margin-top:8px" },
        `Queued for approval: ${a.title} — nothing has been sent. `, el("a", { href: "#approvals", onclick: () => show("approvals"), text: "Review" }))));
    }
    if (payload.rows && payload.rows.length > 1 && window.Charts) {
      const box = el("div", { style: "margin-top:8px;max-width:640px" });
      wrap.append(box);
      // Charts measure text and width, so draw once the message is attached to the page.
      requestAnimationFrame(() => { if (!Charts.auto(box, payload.columns, payload.rows)) box.remove(); });
    }
    if (payload.sql) {
      const d = el("details", {}, el("summary", { text: `Data used (${payload.row_count} rows)` }), el("pre", { text: payload.sql }));
      if (payload.rows && payload.rows.length) {
        const t = el("table"); const cols = payload.columns.map((c) => ({ key: c, label: c }));
        table(t, cols, payload.rows.slice(0, 20)); d.append(el("div", { class: "table-wrap" }, t));
      }
      wrap.append(d);
    }
  }
  $("#log").append(wrap);
  wrap.scrollIntoView({ behavior: "smooth", block: "end" });
  return wrap;
}
const TOOL_LABEL = { search_docs: "Searching documents", run_sql: "Querying data", propose_action: "Preparing an action for approval" };
async function askStream(question, live) {
  // Server-sent events: progress steps, scrubbed text snapshots, then the final payload.
  const r = await fetch("/chat/stream", { method: "POST", headers: headers(), body: JSON.stringify({ question, session_id: sessionId }) });
  if (!r.ok || !r.body) throw new Error("stream unavailable");
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "", final = null;
  const step = (text) => live.progress.append(el("span", { class: "step", text }));
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      if (!chunk.startsWith("data:")) continue;
      const ev = JSON.parse(chunk.slice(5));
      if (ev.type === "route") step(`${ev.intent} · ${ev.tier} tier${ev.local_only ? " · kept local" : ""}`);
      else if (ev.type === "model") step(`${ev.attempt > 1 ? "falling back to " : ""}${ev.model} (${ev.local ? "local" : "cloud"})`);
      else if (ev.type === "tool" && ev.state === "start") step((TOOL_LABEL[ev.tool] || ev.tool) + "…");
      else if (ev.type === "text") { live.bubble.replaceChildren(renderAnswer(ev.text)); live.wrap.scrollIntoView({ block: "end" }); }
      else if (ev.type === "reset") live.bubble.textContent = "…";
      else if (ev.type === "done") final = ev.payload;
      else if (ev.type === "error") throw new Error(ev.message);
    }
  }
  if (!final) throw new Error("no answer received");
  return final;
}
async function ask(question) {
  if (!question) return;
  $("#q").value = ""; $("#send").disabled = true;
  addMsg("user", question);
  const thinking = addMsg("bot", "Thinking…");
  const live = { wrap: thinking, bubble: thinking.querySelector(".bubble"), progress: el("div", { class: "progress" }) };
  thinking.prepend(live.progress);
  try {
    let p;
    try { p = await askStream(question, live); }
    catch (streamErr) { p = await api("/chat", { method: "POST", body: JSON.stringify({ question, session_id: sessionId }) }); }
    thinking.remove(); addMsg(p.route === "refused" ? "bot refused" : "bot", p.text || "(no answer)", p);
    if (p.actions && p.actions.length) refreshHealth();
  } catch (e) { thinking.remove(); addMsg("bot", "Something went wrong: " + e.message); }
  finally { $("#send").disabled = false; $("#q").focus(); }
}
$("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); ask($("#q").value.trim()); });
(async () => { try { const ex = await api("/examples"); ex.examples.forEach((q) => $("#examples").append(el("button", { type: "button", text: q, onclick: () => ask(q) }))); } catch {} })();

// ---------------- DOCUMENTS
async function loadDocs() {
  const d = await api("/documents");
  table($("#doc-table"), [
    { label: "File", render: (r) => r.type === "table" ? el("span", { text: r.file + " (SQL table)" }) : fileLink(r.file, null, r.file) },
    { label: "Kind", render: (r) => el("span", { class: "pill " + (r.category === "invoice" ? "info" : r.category === "spreadsheet" ? "ok" : ""), text: r.category }) },
    { label: "Pages / rows", num: true, render: (r) => r.type === "table" ? `${r.rows} rows` : r.pages },
    { label: "Read by", render: (r) => r.type === "table" ? "spreadsheet" : (r.ocr_pages.length ? el("span", { class: "pill warn", text: "OCR (scan)" }) : "text layer") },
    { label: "Warnings", render: (r) => r.warnings.length ? el("span", { class: "pill bad", text: r.warnings.join(" ") }) : "" },
  ], d.documents);
}
loaders.documents = loadDocs;
const drop = $("#drop"), fileInput = $("#file");
drop.addEventListener("click", () => fileInput.click());
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFiles(e.dataTransfer.files); });
fileInput.addEventListener("change", () => { uploadFiles(fileInput.files); fileInput.value = ""; });
async function uploadFiles(files) {
  const out = $("#upload-results");
  for (const f of files) {
    const card = el("div", { class: "note", style: "margin-top:10px" }, `Processing ${f.name}…`);
    out.prepend(card);
    const fd = new FormData(); fd.append("file", f); fd.append("kind", $("#kind").value);
    try {
      const r = await api("/upload", { method: "POST", body: fd });
      card.replaceChildren(el("strong", { text: fileName(r.file) }), ` — saved and indexed as ${r.classified_as}${r.ocr ? " (read with OCR)" : ""}. `);
      if (r.warnings.length) card.append(el("div", { class: "warnbox", style: "margin-top:6px", text: r.warnings.join(" ") }));
      if (r.invoice) {
        const i = r.invoice;
        card.append(el("div", { class: "small", style: "margin-top:6px" },
          `Supplier: ${i.supplier ?? "MISSING"} · No: ${i.invoice_number ?? "MISSING"} · Date: ${i.invoice_date ?? "MISSING"} · Total: ${money(i.total)} `,
          confBar(i.confidence), " ", el("a", { href: "#invoices", onclick: () => { show("invoices"); openInvoice(i.id); }, text: "Review →" })));
        if (i.issues.length) card.append(el("ul", { class: "issues small" }, i.issues.map((x) => el("li", { text: x }))));
      }
    } catch (e) { card.className = "warnbox"; card.style.marginTop = "10px"; card.textContent = `${f.name}: ${e.message}`; }
  }
  loadDocs(); refreshHealth(); loadInvoices().catch(() => {});
}
$("#reindex").addEventListener("click", async () => {
  const r = await api("/refresh", { method: "POST" });
  $("#reindex-note").textContent = `${r.documents} documents, ${r.doc_chunks} passages, ${r.invoices} invoices indexed.`;
  loadDocs(); refreshHealth();
});

// ---------------- INVOICES
let invoices = [], selectedInvoice = null;
const statusPill = (s) => el("span", { class: "pill " + ({ approved: "ok", rejected: "bad", needs_review: "warn" }[s] || ""), text: s.replace("_", " ") });
async function loadInvoices() {
  invoices = (await api("/invoices")).invoices;
  const n = invoices.filter((i) => i.status === "needs_review").length;
  const badge = $("#n-review"); badge.hidden = !n; badge.textContent = n;
  const f = $("#inv-filter").value;
  const rows = invoices.filter((i) => !f || (f === "flagged" ? i.issues.length : i.status === f));
  table($("#inv-table"), [
    { label: "Status", render: (r) => statusPill(r.status) },
    { label: "Supplier", render: (r) => r.supplier ?? el("span", { class: "pill bad", text: "missing" }) },
    { label: "Invoice no.", render: (r) => r.invoice_number ?? el("span", { class: "pill bad", text: "missing" }) },
    { label: "Date", render: (r) => r.invoice_date ?? el("span", { class: "pill bad", text: "missing" }) },
    { label: "Total", num: true, render: (r) => r.total === null ? el("span", { class: "pill bad", text: "missing" }) : money(r.total) },
    { label: "Confidence", render: (r) => confBar(r.confidence) },
    { label: "Issues", render: (r) => r.issues.length ? el("span", { class: "pill warn", text: r.issues.length + (r.duplicate_of ? " · duplicate" : "") }) : el("span", { class: "pill ok", text: "none" }) },
    { label: "File", render: (r) => el("span", { class: "small muted", text: fileName(r.file) + (r.ocr ? " (OCR)" : "") }) },
  ], rows, (r) => openInvoice(r.id), selectedInvoice);
  if (selectedInvoice) openInvoice(selectedInvoice, false);
}
loaders.invoices = loadInvoices;
$("#inv-filter").addEventListener("change", loadInvoices);
const FIELD_LABELS = { supplier: "Supplier", invoice_number: "Invoice number", invoice_date: "Invoice date", due_date: "Due date", po_number: "PO number", currency: "Currency", subtotal: "Subtotal", tax: "Tax", total: "Total" };
function openInvoice(id, scroll = true) {
  const inv = invoices.find((i) => i.id === id); if (!inv) return;
  selectedInvoice = id;
  document.querySelectorAll("#inv-table tr").forEach((tr) => tr.classList.remove("sel"));
  const box = $("#inv-detail"); box.hidden = false; box.replaceChildren();
  box.append(el("div", { class: "row", style: "justify-content:space-between" },
    el("h3", { style: "margin:0", text: `${inv.supplier ?? "Unknown supplier"} — ${inv.invoice_number ?? "no number"}` }),
    el("div", { class: "row" }, statusPill(inv.status), fileLink(inv.file, null, "Open original ↗"))));
  box.append(el("p", { class: "small muted", text: `Extracted by ${inv.method === "rules+ai" ? "rules + AI (AI values accepted only if printed on the document)" : "local rules"}${inv.ocr ? ", from a scanned image via OCR" : ""}.${inv.reviewed_by ? ` Last reviewed by ${inv.reviewed_by} at ${inv.reviewed_at}.` : ""}` }));
  if (inv.issues.length) box.append(el("div", { class: "warnbox" }, el("strong", { text: "Check before approving:" }), el("ul", { class: "issues" }, inv.issues.map((x) => el("li", { text: x })))));
  const grid = el("div", { class: "fields", style: "margin-top:12px" });
  const inputs = {};
  for (const [k, label] of Object.entries(FIELD_LABELS)) {
    const v = inv[k];
    const inp = el("input", { value: v ?? "", placeholder: v === null ? "not found — enter if known" : "" });
    if (v === null && ["supplier", "invoice_number", "invoice_date", "total"].includes(k)) inp.style.borderColor = "var(--bad)";
    inputs[k] = inp;
    const required = ["supplier", "invoice_number", "invoice_date", "total"].includes(k);
    grid.append(el("label", { class: "small", text: label }), inp, el("span", { class: "confcell" }, v === null ? el("span", { class: "pill " + (required ? "bad" : ""), text: required ? "missing" : "not on document" }) : confBar(inv.field_confidence[k])));
    const ev = [inv.field_notes[k], inv.field_evidence[k] ? `from: “${inv.field_evidence[k].slice(0, 90)}”` : ""].filter(Boolean).join(" · ");
    if (ev) grid.append(el("div", { class: "ev", text: ev }));
  }
  box.append(grid);
  if (inv.line_items && inv.line_items.length) {
    const lt = el("table", { style: "margin-top:12px" });
    table(lt, [
      { label: "Line item", key: "description" }, { label: "Qty", num: true, render: (r) => r.quantity },
      { label: "Unit price", num: true, render: (r) => r.unit_price === null ? "—" : money(r.unit_price) },
      { label: "Amount", num: true, render: (r) => money(r.amount) },
    ], inv.line_items);
    box.append(el("h3", { text: `Line items (${inv.line_items.length})` }), el("div", { class: "table-wrap" }, lt));
  }
  const note = el("input", { placeholder: "Review note (optional)", style: "flex:1;min-width:200px" });
  const send = async (status) => {
    const name = reviewer(); if (!name) return;
    const corrections = {};
    for (const [k, inp] of Object.entries(inputs)) { const orig = inv[k] ?? ""; if (inp.value.trim() !== String(orig)) corrections[k] = inp.value.trim(); }
    try {
      await api(`/invoices/${id}/review`, { method: "POST", body: JSON.stringify({ status, reviewer: name, note: note.value, corrections }) });
      toast(`Invoice ${status.replace("_", " ")}${Object.keys(corrections).length ? " with corrections" : ""}. Logged.`);
      loadInvoices();
    } catch (e) { toast(e.message); }
  };
  box.append(el("div", { class: "row", style: "margin-top:14px" }, note,
    el("button", { class: "btn", text: "Save corrections", onclick: () => send("needs_review") }),
    el("button", { class: "btn danger", text: "Reject", onclick: () => send("rejected") }),
    el("button", { class: "btn good", text: "Approve", onclick: () => send("approved") })));
  if (scroll) box.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ---------------- QUICKBOOKS
const SEV = { issue: "bad", warning: "warn", ok: "ok" };
async function loadQbo() {
  const s = await api("/qbo/status");
  const card = $("#qbo-card"); card.replaceChildren();
  const modeText = s.mode === "mock" ? "Offline test company (bundled fixture in QuickBooks API format)" : s.mode === "off" ? "Disabled" : `Intuit ${s.mode} company`;
  card.append(el("div", { class: "row", style: "justify-content:space-between" },
    el("div", {}, el("strong", { text: s.company || (s.connected ? "Connected" : "Not connected") }),
      el("div", { class: "small muted", text: `${modeText}${s.last_sync ? " · last synced " + s.last_sync : ""}` })),
    el("div", { class: "row" },
      s.mode === "sandbox" && !s.connected ? el("a", { class: "btn primary", href: "/qbo/connect", style: "text-decoration:none", text: "Connect sandbox company" }) : null,
      s.connected ? el("button", { class: "btn primary", text: "Sync now", onclick: async () => { try { const r = await api("/qbo/sync", { method: "POST" }); toast(`Synced (read-only): ${Object.entries(r.counts).map(([k, v]) => `${v} ${k.replace("qbo_", "")}`).join(", ")}`); loadQbo(); refreshHealth(); } catch (e) { toast(e.message); } } }) : null,
      s.connected ? el("button", { class: "btn danger", text: s.mode === "mock" ? "Remove local copy" : "Disconnect & revoke", onclick: async () => { if (!confirm("Revoke access and delete the local QuickBooks copy?")) return; await api("/qbo/disconnect", { method: "POST" }); toast("Disconnected; local copy deleted."); loadQbo(); } }) : null)));
  if (s.mode === "sandbox" && s.configured === false) card.append(el("div", { class: "warnbox", style: "margin-top:10px", text: "Set QBO_CLIENT_ID and QBO_CLIENT_SECRET from an Intuit developer sandbox app, then restart. See docs/QUICKBOOKS.md." }));

  const recon = (await api("/reconciliation")).rows;
  table($("#recon-table"), [
    { label: "Result", render: (r) => el("span", { class: "pill " + SEV[r.severity], text: r.status.replaceAll("_", " ") }) },
    { label: "Supplier", key: "supplier" },
    { label: "Invoice no.", key: "invoice_number" },
    { label: "Document", num: true, render: (r) => money(r.document_total) },
    { label: "QuickBooks", num: true, render: (r) => money(r.qbo_total) },
    { label: "Detail", render: (r) => el("span", { class: "small", text: r.detail }) },
    { label: "", render: (r) => {
      if (r.status === "not_in_quickbooks") return el("button", { class: "btn", text: "Propose: record bill", onclick: (e) => { e.stopPropagation(); proposeBill(r); } });
      if (r.status === "amount_mismatch") return el("button", { class: "btn", text: "Propose: query supplier", onclick: (e) => { e.stopPropagation(); proposeMismatch(r); } });
      return r.file ? fileLink(r.file, null, "open") : "";
    } },
  ], recon);

  const bank = (await api("/bank-reconciliation")).rows;
  table($("#bank-table"), [
    { label: "Result", render: (r) => el("span", { class: "pill " + SEV[r.severity], text: r.status.replaceAll("_", " ") }) },
    { label: "Date", render: (r) => r.date || "—" },
    { label: "Bank line", render: (r) => r.description || el("span", { class: "muted", text: "(not on statement)" }) },
    { label: "Amount", num: true, render: (r) => r.amount === null ? "—" : money(r.amount) },
    { label: "Detail", render: (r) => el("span", { class: "small", text: r.detail }) },
  ], bank.filter((r) => r.status !== "opening_balance"));
  const sum = await api("/reports/summary");
  const md = $("#summary"); md.replaceChildren();
  sum.markdown.split("\n").forEach((line) => {
    if (line.startsWith("# ")) md.append(el("h1", { text: line.slice(2) }));
    else if (line.startsWith("## ")) md.append(el("h2", { text: line.slice(3) }));
    else if (/^\s*- /.test(line)) md.append(el("div", { style: `padding-left:${line.startsWith("  ") ? 26 : 12}px` }, "• ", renderAnswer(line.replace(/^\s*- /, ""))));
    else if (line.trim()) md.append(el("p", { class: "small muted", text: line.replaceAll("_", "") }));
  });
  const data = await api("/qbo/data");
  table($("#bills-table"), [
    { label: "Vendor", key: "vendor_name" }, { label: "No.", key: "doc_number" }, { label: "Due", key: "due_date" },
    { label: "Balance", num: true, render: (r) => money(r.balance) },
    { label: "", render: (r) => r.days_overdue ? el("span", { class: "pill bad", text: r.days_overdue + "d overdue" }) : "" },
  ], (data.bills || []).filter((b) => b.balance > 0));
  table($("#ar-table"), [
    { label: "Customer", key: "customer_name" }, { label: "No.", key: "doc_number" },
    { label: "Balance", num: true, render: (r) => money(r.balance) },
    { label: "", render: (r) => r.days_overdue ? el("span", { class: "pill bad", text: r.days_overdue + "d overdue" }) : el("span", { class: "pill ok", text: r.balance ? "open" : "paid" }) },
  ], data.invoices || []);
}
loaders.quickbooks = loadQbo;
async function proposeBill(r) {
  const details = `Record a bill in QuickBooks:\n  Vendor: ${r.supplier}\n  Bill no.: ${r.invoice_number}\n  Date: ${r.invoice_date}\n  Amount: ${money(r.document_total)}\n  Source document: ${r.file}\n\nCheck the invoice is approved under the expense policy before recording.`;
  await api("/approvals", { method: "POST", body: JSON.stringify({ action_type: "record_bill", title: `Record ${r.supplier} ${r.invoice_number} (${money(r.document_total)}) in QuickBooks`, details, dedupe_key: "bill:" + r.invoice_id }) });
  toast("Proposed. It now waits in Approvals — nothing was written to QuickBooks."); refreshHealth();
}
async function proposeMismatch(r) {
  const details = `Draft email to ${r.supplier}:\n\nHello,\n\nWe are reconciling invoice ${r.invoice_number}. Your invoice shows ${money(r.document_total)} while our records show ${money(r.qbo_total)}. Could you please confirm the correct amount?\n\nThank you.`;
  await api("/approvals", { method: "POST", body: JSON.stringify({ action_type: "draft_email", title: `Ask ${r.supplier} to confirm invoice ${r.invoice_number} amount`, details, dedupe_key: "mismatch:" + r.invoice_id }) });
  toast("Draft email queued for approval. Nothing was sent."); refreshHealth();
}

// ---------------- APPROVALS
async function loadApprovals() {
  const items = (await api("/approvals")).items;
  const box = $("#approval-list"); box.replaceChildren();
  if (!items.length) box.append(el("div", { class: "card muted", text: "Nothing proposed yet. Try “Propose” on the QuickBooks tab, or ask the assistant to draft an email." }));
  for (const it of items) {
    const note = el("input", { placeholder: "Note (optional)", style: "flex:1;min-width:180px" });
    const decide = async (decision) => {
      const name = reviewer(); if (!name) return;
      try { await api(`/approvals/${it.id}/decision`, { method: "POST", body: JSON.stringify({ decision, reviewer: name, note: note.value }) }); toast(decision === "approved" ? "Approved and logged (not executed in this prototype)." : "Rejected and logged."); loadApprovals(); refreshHealth(); }
      catch (e) { toast(e.message); }
    };
    box.append(el("div", { class: "card" },
      el("div", { class: "row", style: "justify-content:space-between" }, el("strong", { text: it.title }),
        el("span", { class: "pill " + ({ pending: "warn", approved: "ok", rejected: "bad" }[it.status]), text: it.status })),
      el("div", { class: "small muted", text: `${it.action_label} · proposed by ${it.proposed_by} · ${it.created_at}` }),
      el("pre", { text: it.details }),
      it.status === "pending"
        ? el("div", { class: "row" }, note, el("button", { class: "btn danger", text: "Reject", onclick: () => decide("rejected") }), el("button", { class: "btn good", text: "Approve", onclick: () => decide("approved") }))
        : el("div", { class: "small" }, el("strong", { text: `${it.status} by ${it.decided_by} at ${it.decided_at}. ` }), it.execution, it.decision_note ? ` Note: ${it.decision_note}` : "")));
  }
}
loaders.approvals = loadApprovals;

// ---------------- ACTIVITY
async function loadAudit() {
  const a = await api("/audit?limit=300");
  const i = a.integrity;
  $("#integrity").replaceChildren(el("span", { class: "pill " + (i.ok ? "ok" : "bad"), text: i.ok ? `Integrity verified · ${i.entries} entries` : `Log altered at entry ${i.broken_at}` }));
  table($("#audit-table"), [
    { label: "Time (UTC)", render: (r) => el("span", { class: "small", style: "white-space:nowrap", text: r.ts.replace("T", " ").replace("+00:00", "") }) },
    { label: "Event", render: (r) => el("span", { class: "pill info", text: r.event }) },
    { label: "Who", key: "actor" },
    { label: "Details", render: (r) => el("span", { class: "small muted", text: Object.entries(r.details || {}).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length)).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ").slice(0, 300) }) },
  ], a.entries);
}
loaders.activity = loadAudit;
$("#audit-refresh").addEventListener("click", loadAudit);

// ---------------- PRIVACY
async function loadPrivacy() {
  const p = await api("/privacy");
  $("#model-note").textContent = `AI model: ${p.model_note} Cloud AI is ${p.cloud_ai_allowed ? "ALLOWED (approved)" : "blocked until approved"}.`;
  table($("#privacy-table"), [
    { label: "Function", key: "function" }, { label: "Runs", key: "runs" },
    { label: "Needs internet", render: (r) => el("span", { class: "pill " + (r.internet ? "warn" : "ok"), text: r.internet ? "yes" : "no" }) },
    { label: "What leaves this machine", key: "leaves_machine" },
  ], p.functions);
  $("#controls").replaceChildren(...p.controls.map((c) => el("li", { text: c })));
}
loaders.privacy = loadPrivacy;

// ---------------- OVERVIEW (dashboard)
let lastDash = null;
function renderDash(d) {
  const F = Charts.fmt;
  $("#overview-sub").textContent = `Where things stand as of ${d.as_of}, from local data. Hover or focus any chart for values; "Show table" gives the numbers.`;
  const tone = { good: ["var(--status-good)", "✓ OK"], warn: ["var(--status-warning)", "! Check"], bad: ["var(--status-critical)", "✕ Attention"] };
  $("#kpis").replaceChildren(...d.kpis.map((k) => {
    const t = tone[k.tone];
    const detail = el("div", { class: "detail" });
    if (t) { const dot = el("span", { class: "status-dot" }); dot.style.background = t[0]; detail.append(el("span", { class: "status" }, dot, t[1])); }
    detail.append(el("span", { text: k.detail || "" }));
    return el("div", { class: "kpi" }, el("div", { class: "label", text: k.label }),
      el("div", { class: "value", text: k.unit === "money" ? F.money(k.value) : F.count(k.value) }), detail);
  }));
  const aging = (id, rows, empty) => Charts.columns($(id), { categories: rows.map((b) => b.bucket), series: [{ name: "Balance", color: "var(--series-1)", values: rows.map((b) => b.amount) }],
    format: F.money2, notes: rows.map((b) => `${b.count} item${b.count === 1 ? "" : "s"}`), categoryName: "Days overdue", empty,
    shortCategories: rows.map((b) => b.bucket.replace("Not yet due", "Not due").replace(" days", "")) });
  aging("#ch-ap", d.ap_aging, "Sync QuickBooks to see payables.");
  aging("#ch-ar", d.ar_aging, "Sync QuickBooks to see receivables.");
  const m = d.cash_flow.months;
  Charts.columns($("#ch-flow"), { categories: m.map((x) => x.month), series: [
    { name: "Money in", color: "var(--series-1)", values: m.map((x) => x.money_in) },
    { name: "Money out", color: "var(--series-2)", values: m.map((x) => x.money_out) }], format: F.money2, categoryName: "Month", empty: "Add a bank statement spreadsheet." });
  Charts.line($("#ch-balance"), { points: d.cash_flow.balance.map((b) => ({ x: b.date, y: b.balance })), format: F.money2, xName: "Date", yName: "Balance", empty: "Add a bank statement with a balance column." });
  Charts.barH($("#ch-spend"), { rows: d.spend_by_supplier.map((r) => ({ label: r.supplier, value: r.amount, note: `${r.bills} bill${r.bills === 1 ? "" : "s"}` })), format: F.compact, tipFormat: F.money2, labelName: "Supplier", valueName: "Billed", empty: "Sync QuickBooks to see spend." });
  const seg = (rows) => ["ok", "warning", "issue"].map((sev) => ({ status: sev, label: rows.filter((r) => r.severity === sev).map((r) => r.status.replaceAll("_", " ")).join(", ") || sev,
    value: rows.filter((r) => r.severity === sev).reduce((a, r) => a + r.items, 0) }));
  Charts.stacked($("#ch-recon"), { rows: [{ label: "Invoices vs QuickBooks", segments: seg(d.invoice_reconciliation) }, { label: "Bank vs QuickBooks", segments: seg(d.bank_reconciliation) }], empty: "Sync QuickBooks to reconcile." });
  Charts.bullet($("#ch-budget"), { rows: d.budget.map((r) => ({ label: r.item, budget: r.budget, committed: r.committed, spent: r.spent })), empty: "Add a project budget spreadsheet (columns: line_item, budget, committed, spent)." });
  Charts.barH($("#ch-conf"), { rows: d.invoice_confidence.slice(0, 12).map((r) => ({ label: `${r.supplier || "Unknown"} ${r.invoice_number || "(no number)"}`, value: r.confidence, note: r.status.replace("_", " ") })),
    format: F.pct, labelName: "Invoice", valueName: "Confidence", empty: "No invoices yet." });
}
async function loadOverview() { lastDash = await api("/dashboard"); renderDash(lastDash); }
loaders.overview = loadOverview;
let resizeT;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { if (lastDash && $("#tab-overview").classList.contains("active")) renderDash(lastDash); }, 150); });

// ---------------- REPORTS
function renderMarkdown(md, target) {
  target.replaceChildren();
  const lines = md.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("|") && lines[i + 1] && /^\|[-|]+\|?$/.test(lines[i + 1])) {
      const tbl = el("table"); const head = line.split("|").slice(1, -1).map((c) => c.trim());
      tbl.append(el("tr", {}, head.map((c) => el("th", { text: c })))); i += 2;
      while (i < lines.length && lines[i].startsWith("|")) { tbl.append(el("tr", {}, lines[i].split("|").slice(1, -1).map((c) => el("td", {}, renderAnswer(c.trim()))))); i++; }
      i--; target.append(el("div", { class: "table-wrap" }, tbl)); continue;
    }
    if (line.startsWith("### ")) target.append(el("h3", { text: line.slice(4) }));
    else if (line.startsWith("## ")) target.append(el("h2", { text: line.slice(3) }));
    else if (line.startsWith("# ")) target.append(el("h1", { text: line.slice(2) }));
    else if (line.startsWith("> ")) target.append(el("blockquote", {}, renderAnswer(line.slice(2))));
    else if (line.startsWith("- ")) target.append(el("div", { style: "padding-left:12px" }, "• ", renderAnswer(line.slice(2))));
    else if (line.trim()) target.append(el("p", { class: line.startsWith("_") ? "small muted" : "", text: line.replace(/^_|_$/g, "") }));
  }
}
async function openReport(id) {
  const box = $("#report-view"); box.replaceChildren(el("div", { class: "muted", text: "Building report…" }));
  const r = await api("/reports/" + id);
  const md = el("div", { class: "md" });
  const aiBox = el("div");
  const aiBtn = el("button", { class: "btn", text: "Add AI summary", onclick: async () => {
    aiBtn.disabled = true; aiBtn.textContent = "Summarising…";
    try {
      const s2 = await api(`/reports/${id}/summary`, { method: "POST" });
      aiBox.replaceChildren(s2.summary ? el("div", { class: "warnbox", style: "margin:10px 0" }, el("strong", { text: `AI-written summary (${s2.model}${s2.local_only ? ", local" : ""}) — check against the figures below:` }), el("div", { style: "white-space:pre-wrap;margin-top:6px", text: s2.summary }))
        : el("div", { class: "note", style: "margin:10px 0", text: s2.note || "No summary available." }));
    } catch (e) { toast(e.message); }
    aiBtn.textContent = "Add AI summary"; aiBtn.disabled = false;
  } });
  box.replaceChildren(el("div", { class: "row", style: "justify-content:flex-end;gap:8px" }, aiBtn,
    el("a", { class: "btn", href: `/reports/${id}.html`, target: "_blank", rel: "noopener", style: "text-decoration:none", text: "Print / PDF" }),
    el("a", { class: "btn", href: `/reports/${id}.md`, style: "text-decoration:none", text: "Download .md" })), aiBox, md);
  renderMarkdown(r.markdown, md);
}
async function loadReports() {
  const list = (await api("/reports")).reports;
  $("#report-list").replaceChildren(...list.map((r) => el("button", { class: "btn", title: r.description, text: r.title, onclick: () => openReport(r.id) })));
}
loaders.reports = loadReports;

// ---------------- AI MODELS
async function loadModels() {
  const r = await api("/router");
  $("#router-note").textContent = r.note;
  const pv = r.privacy;
  $("#router-privacy").textContent = `Cloud AI: ${pv.cloud_ai_allowed ? "allowed" : "blocked"} · data that may go to cloud models: ${pv.cloud_allowed_data.join(", ") || "none"} · PII masking: ${pv.redact_pii ? "on" : "off"} · local model: ${pv.local_model_available ? "available" : "not running"}`;
  const byName = Object.fromEntries(r.models.map((m) => [m.model, m]));
  for (const tier of ["strong", "fast"]) {
    const ol = $("#tier-" + tier); ol.replaceChildren();
    if (!r.tiers[tier].length) ol.append(el("li", { class: "muted", text: "none configured" }));
    r.tiers[tier].forEach((name) => { const m = byName[name] || {}; ol.append(el("li", {}, name, " ", el("span", { class: "pill " + (m.local ? "ok" : "warn"), text: m.local ? "local" : "cloud" }), m.circuit_open ? el("span", { class: "pill bad", text: "paused after failures" }) : "")); });
  }
  table($("#models-table"), [
    { label: "Model", key: "model" },
    { label: "Where", render: (m) => el("span", { class: "pill " + (m.local ? "ok" : "warn"), text: m.local ? "local" : "cloud" }) },
    { label: "Calls", num: true, key: "calls" }, { label: "Failures", num: true, key: "failures" },
    { label: "Avg ms", num: true, render: (m) => m.avg_ms ?? "—" },
    { label: "Tokens in/out", num: true, render: (m) => `${m.input_tokens.toLocaleString()} / ${m.output_tokens.toLocaleString()}` },
    { label: "Est. cost", num: true, render: (m) => m.local ? "$0" : (m.priced ? "$" + m.cost_usd.toFixed(4) : "set LLM_PRICING") },
    { label: "Last error", render: (m) => el("span", { class: "small muted", text: m.last_error || "" }) },
  ], r.models);
  table($("#providers-table"), [
    { label: "Provider", key: "label" }, { label: "Spec prefix", render: (p) => p.provider + ":" },
    { label: "API key", render: (p) => p.configured === null ? el("span", { class: "muted", text: "n/a" }) : el("span", { class: "pill " + (p.configured ? "ok" : ""), text: p.configured ? "set" : "not set" }) },
  ], r.providers);
  $("#router-notes").replaceChildren(...r.notes.map((n) => el("li", { text: n })));
}
loaders.models = loadModels;

// ---------------- boot
refreshHealth(); setInterval(refreshHealth, 15000);
loadInvoices().catch(() => {});
const start = (location.hash || "#overview").slice(1);
show(document.getElementById("tab-" + start) ? start : "overview");

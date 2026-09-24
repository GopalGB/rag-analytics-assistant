/* Minimal, dependency-free SVG charts for the assistant UI (works fully offline).
 *
 * Forms:  barH (ranked magnitudes) · columns (ordered buckets / 1–2 series) · line (trend over time)
 *         stacked (part-to-whole status) · bullet (budget vs committed vs spent)
 * Spec:   bars <= 24px with 4px rounded data-end, square at the baseline; 2px surface gap between
 *         touching bars; 2px lines; end dots r=4 with a 2px surface ring; hairline solid grid;
 *         text in ink tokens (never series colours); legend for >= 2 series; every chart has a
 *         hover + keyboard-focus tooltip and a "Table" view so no value is gated behind hover.
 * Colour: categorical slots 1–2 (validated for CVD on this app's light and dark surfaces), status
 *         colours only for meaning (ok / warning / issue), always paired with an icon and a label.
 * Safety: all labels are inserted with textContent (they can come from documents and QuickBooks).
 */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  let tip;

  function s(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) if (v !== undefined && v !== null) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }
  function h(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  const fmt = {
    money: (v) => (v === null || v === undefined ? "—" : "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 })),
    money2: (v) => (v === null || v === undefined ? "—" : "$" + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })),
    compact: (v) => {
      const a = Math.abs(v);
      if (a >= 1e6) return "$" + (v / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
      if (a >= 1e3) return "$" + (v / 1e3).toFixed(a >= 1e4 ? 0 : 1).replace(/\.0$/, "") + "K";
      return "$" + Math.round(v);
    },
    count: (v) => Number(v).toLocaleString(),
    pct: (v) => Math.round(v * 100) + "%",
    num: (v) => Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }),
  };

  // ---------------------------------------------------------------- tooltip
  function tooltip() {
    if (!tip) {
      tip = h("div", "viz-tip");
      tip.setAttribute("role", "status");
      document.body.appendChild(tip);
    }
    return tip;
  }
  function showTip(evt, rows, anchorEl) {
    const t = tooltip();
    t.replaceChildren();
    rows.forEach((r) => {
      const line = h("div", "viz-tip-row");
      if (r.color) {
        const key = h("span", "viz-key");
        key.style.background = r.color;
        line.appendChild(key);
      }
      line.appendChild(h("strong", null, r.value));
      if (r.label) line.appendChild(h("span", "viz-tip-label", r.label));
      t.appendChild(line);
    });
    t.style.display = "block";
    let x, y;
    if (evt && evt.clientX !== undefined && evt.type !== "focus") {
      x = evt.clientX; y = evt.clientY;
    } else {
      const b = anchorEl.getBoundingClientRect();
      x = b.left + b.width / 2; y = b.top;
    }
    const w = t.offsetWidth, hh = t.offsetHeight;
    t.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 12)) + "px";
    t.style.top = Math.max(8, y - hh - 12) + "px";
  }
  function hideTip() { if (tip) tip.style.display = "none"; }
  function hover(target, rowsFn, markEl) {
    target.setAttribute("tabindex", "0");
    const on = (e) => { showTip(e, rowsFn(), target); if (markEl) markEl.classList.add("viz-hot"); };
    const off = () => { hideTip(); if (markEl) markEl.classList.remove("viz-hot"); };
    target.addEventListener("pointermove", on);
    target.addEventListener("focus", on);
    target.addEventListener("pointerleave", off);
    target.addEventListener("blur", off);
  }

  // ---------------------------------------------------------------- helpers
  function niceMax(max) {
    if (max <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(max)));
    for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= max) return m * p;
    return 10 * p;
  }
  function textWidth(svg, text, cls) {
    const t = s("text", { class: cls, x: -9999, y: -9999 }, svg);
    t.textContent = text;
    const w = t.getComputedTextLength();
    t.remove();
    return w;
  }
  function fitText(svg, text, cls, maxW) {
    // Measure, then shorten with an ellipsis until it fits — labels are never clipped mid-glyph.
    if (textWidth(svg, text, cls) <= maxW) return text;
    let t = text;
    while (t.length > 3 && textWidth(svg, t + "…", cls) > maxW) t = t.slice(0, -1);
    return t.trimEnd() + "…";
  }
  // bar path: square at the baseline, 4px rounded at the data end
  function barPathH(x, y, w, hgt, r = 4) {
    r = Math.min(r, w / 2, hgt / 2);
    if (w <= 0) return "";
    return `M${x},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt - r}Q${x + w},${y + hgt} ${x + w - r},${y + hgt}H${x}Z`;
  }
  function barPathV(x, y, w, hgt, r = 4) { // y = top, grows up from baseline y+hgt
    r = Math.min(r, w / 2, hgt / 2);
    if (hgt <= 0) return "";
    return `M${x},${y + hgt}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + hgt}Z`;
  }
  function frame(el, height, spec) {
    el.replaceChildren();
    el.classList.add("viz");
    const width = Math.max(260, el.clientWidth || 480);
    const svg = s("svg", { viewBox: `0 0 ${width} ${height}`, width: "100%", height, role: "img",
      "aria-label": spec.title || "chart" }, el);
    return { svg, width };
  }
  function legend(el, items) {
    const box = h("div", "viz-legend");
    items.forEach((it) => {
      const item = h("span", "viz-legend-item");
      const sw = h("span", it.line ? "viz-key viz-key-line" : "viz-key");
      sw.style.background = it.color;
      item.appendChild(sw);
      if (it.icon) item.appendChild(h("span", "viz-icon", it.icon));
      item.appendChild(document.createTextNode(it.label));
      box.appendChild(item);
    });
    el.appendChild(box);
  }
  function tableView(el, headers, rows) {
    const wrap = h("div", "viz-table");
    const btn = h("button", "viz-table-btn", "Show table");
    btn.type = "button";
    const tbl = h("table");
    tbl.hidden = true;
    const tr = h("tr");
    headers.forEach((c) => tr.appendChild(h("th", null, c)));
    tbl.appendChild(tr);
    rows.forEach((r) => {
      const row = h("tr");
      r.forEach((c) => row.appendChild(h("td", null, String(c))));
      tbl.appendChild(row);
    });
    btn.addEventListener("click", () => { tbl.hidden = !tbl.hidden; btn.textContent = tbl.hidden ? "Show table" : "Hide table"; });
    wrap.append(btn, tbl);
    el.appendChild(wrap);
  }
  function empty(el, msg) { el.replaceChildren(h("div", "viz-empty", msg || "No data yet.")); }

  // ---------------------------------------------------------------- horizontal bars (ranked magnitudes)
  function barH(el, spec) {
    const rows = (spec.rows || []).filter((r) => r.value !== null && r.value !== undefined);
    if (!rows.length) return empty(el, spec.empty);
    const f = spec.format || fmt.num;
    const band = 30, thick = 16, top = 6;
    const { svg, width } = frame(el, rows.length * band + top + 4, spec);
    const labelW = Math.min(width * 0.42, Math.max(...rows.map((r) => textWidth(svg, r.label, "viz-label"))) + 10);
    const valueW = Math.max(...rows.map((r) => textWidth(svg, f(r.value), "viz-value"))) + 8;
    const plotW = Math.max(40, width - labelW - valueW - 4);
    const max = niceMax(Math.max(...rows.map((r) => r.value)));
    s("line", { x1: labelW, x2: labelW, y1: top - 2, y2: rows.length * band + top, class: "viz-axis" }, svg);
    rows.forEach((r, i) => {
      const y = top + i * band;
      const w = Math.max(0, (r.value / max) * plotW);
      const t = s("text", { x: labelW - 8, y: y + band / 2, "text-anchor": "end", "dominant-baseline": "middle", class: "viz-label" }, svg);
      t.textContent = fitText(svg, r.label, "viz-label", labelW - 10);
      const mark = s("path", { d: barPathH(labelW, y + (band - thick) / 2, w, thick), fill: r.color || spec.color || "var(--series-1)", class: "viz-mark" }, svg);
      const v = s("text", { x: labelW + w + 6, y: y + band / 2, "dominant-baseline": "middle", class: "viz-value" }, svg);
      v.textContent = f(r.value);
      const hit = s("rect", { x: 0, y, width, height: band, class: "viz-hit" }, svg);
      hover(hit, () => [{ value: (spec.tipFormat || f)(r.value), label: r.label + (r.note ? " · " + r.note : "") }], mark);
    });
    tableView(el, [spec.labelName || "Item", spec.valueName || "Value"], rows.map((r) => [r.label, (spec.tipFormat || f)(r.value)]));
  }

  // ---------------------------------------------------------------- columns (ordered categories; 1–2 series)
  function columns(el, spec) {
    const cats = spec.categories || [];
    const series = spec.series || [];
    if (!cats.length || !series.length) return empty(el, spec.empty);
    const f = spec.format || fmt.num;
    const height = spec.height || 210, bottom = 34, left = 48, top = 18;
    const { svg, width } = frame(el, height, spec);
    const plotH = height - bottom - top, plotW = width - left - 8;
    const max = niceMax(Math.max(1e-9, ...series.flatMap((sr) => sr.values)));
    for (let i = 0; i <= 4; i++) {
      const y = top + plotH - (i / 4) * plotH;
      s("line", { x1: left, x2: width - 8, y1: y, y2: y, class: i ? "viz-grid" : "viz-axis" }, svg);
      const tl = s("text", { x: left - 6, y, "text-anchor": "end", "dominant-baseline": "middle", class: "viz-tick" }, svg);
      tl.textContent = (spec.tickFormat || fmt.compact)((max * i) / 4);
    }
    const slot = plotW / cats.length;
    const barW = Math.min(24, (slot * 0.7 - 2 * (series.length - 1)) / series.length);
    const groupW = barW * series.length + 2 * (series.length - 1);
    cats.forEach((c, ci) => {
      const gx = left + ci * slot + (slot - groupW) / 2;
      const cl = s("text", { x: left + ci * slot + slot / 2, y: height - bottom + 16, "text-anchor": "middle", class: "viz-tick" }, svg);
      const short = spec.shortCategories && textWidth(svg, c, "viz-tick") > slot - 6 ? spec.shortCategories[ci] : c;
      cl.textContent = fitText(svg, short, "viz-tick", slot - 6);
      const marks = [];
      series.forEach((sr, si) => {
        const v = sr.values[ci] || 0;
        const hgt = (v / max) * plotH;
        marks.push(s("path", { d: barPathV(gx + si * (barW + 2), top + plotH - hgt, barW, hgt), fill: sr.color, class: "viz-mark" }, svg));
        if (series.length === 1 && v > 0) {
          const vl = s("text", { x: gx + barW / 2, y: top + plotH - hgt - 5, "text-anchor": "middle", class: "viz-value" }, svg);
          vl.textContent = (spec.capFormat || fmt.compact)(v);
        }
      });
      const hit = s("rect", { x: left + ci * slot, y: top, width: slot, height: plotH + bottom, class: "viz-hit" }, svg);
      const g = { classList: { add: () => marks.forEach((m) => m.classList.add("viz-hot")), remove: () => marks.forEach((m) => m.classList.remove("viz-hot")) } };
      hover(hit, () => series.map((sr) => ({ value: f(sr.values[ci] || 0), label: series.length > 1 ? `${sr.name} · ${c}` : c + (spec.notes ? " · " + spec.notes[ci] : ""), color: series.length > 1 ? sr.color : null })), g);
    });
    if (series.length > 1) legend(el, series.map((sr) => ({ label: sr.name, color: sr.color })));
    tableView(el, [spec.categoryName || "Category", ...series.map((sr) => sr.name)], cats.map((c, i) => [c, ...series.map((sr) => f(sr.values[i] || 0))]));
  }

  // ---------------------------------------------------------------- line (single series over time)
  function line(el, spec) {
    const pts = (spec.points || []).filter((p) => p.y !== null && p.y !== undefined);
    if (pts.length < 2) return empty(el, spec.empty);
    const f = spec.format || fmt.num;
    const height = spec.height || 210, bottom = 28, left = 52, top = 16, right = 70;
    const { svg, width } = frame(el, height, spec);
    const plotH = height - bottom - top, plotW = width - left - right;
    const xs = pts.map((p) => new Date(p.x).getTime());
    const x0 = Math.min(...xs), x1 = Math.max(...xs);
    const ys = pts.map((p) => p.y);
    const lo = Math.min(0, ...ys), max = niceMax(Math.max(...ys));
    const X = (t) => left + ((t - x0) / Math.max(1, x1 - x0)) * plotW;
    const Y = (v) => top + plotH - ((v - lo) / (max - lo)) * plotH;
    for (let i = 0; i <= 4; i++) {
      const v = lo + ((max - lo) * i) / 4;
      s("line", { x1: left, x2: left + plotW, y1: Y(v), y2: Y(v), class: i ? "viz-grid" : "viz-axis" }, svg);
      const tl = s("text", { x: left - 6, y: Y(v), "text-anchor": "end", "dominant-baseline": "middle", class: "viz-tick" }, svg);
      tl.textContent = (spec.tickFormat || fmt.compact)(v);
    }
    [0, pts.length - 1].forEach((i) => {
      const t = s("text", { x: X(xs[i]), y: height - 8, "text-anchor": i ? "end" : "start", class: "viz-tick" }, svg);
      t.textContent = pts[i].x;
    });
    const color = spec.color || "var(--series-1)";
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(xs[i])},${Y(p.y)}`).join("");
    s("path", { d: `${d}L${X(xs[xs.length - 1])},${Y(lo)}L${X(xs[0])},${Y(lo)}Z`, fill: color, "fill-opacity": 0.1, stroke: "none" }, svg);
    s("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    const last = pts.length - 1;
    s("circle", { cx: X(xs[last]), cy: Y(pts[last].y), r: 4, fill: color, stroke: "var(--viz-surface)", "stroke-width": 2 }, svg);
    const el2 = s("text", { x: X(xs[last]) + 8, y: Y(pts[last].y), "dominant-baseline": "middle", class: "viz-value" }, svg);
    el2.textContent = (spec.capFormat || fmt.compact)(pts[last].y);
    const cross = s("line", { x1: 0, x2: 0, y1: top, y2: top + plotH, class: "viz-cross", visibility: "hidden" }, svg);
    const dot = s("circle", { r: 4, fill: color, stroke: "var(--viz-surface)", "stroke-width": 2, visibility: "hidden" }, svg);
    const hit = s("rect", { x: left, y: top, width: plotW, height: plotH, class: "viz-hit" }, svg);
    let idx = last;
    const nearest = (e) => {
      const b = svg.getBoundingClientRect();
      const px = ((e.clientX - b.left) / b.width) * width;
      let best = 0;
      xs.forEach((t, i) => { if (Math.abs(X(t) - px) < Math.abs(X(xs[best]) - px)) best = i; });
      return best;
    };
    const place = (i) => {
      idx = i;
      cross.setAttribute("x1", X(xs[i])); cross.setAttribute("x2", X(xs[i])); cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", X(xs[i])); dot.setAttribute("cy", Y(pts[i].y)); dot.setAttribute("visibility", "visible");
    };
    hit.setAttribute("tabindex", "0");
    hit.addEventListener("pointermove", (e) => { place(nearest(e)); showTip(e, [{ value: f(pts[idx].y), label: pts[idx].x + (pts[idx].note ? " · " + pts[idx].note : "") }], hit); });
    hit.addEventListener("focus", () => { place(idx); showTip(null, [{ value: f(pts[idx].y), label: pts[idx].x }], hit); });
    hit.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        place(Math.max(0, Math.min(last, idx + (e.key === "ArrowRight" ? 1 : -1))));
        showTip(null, [{ value: f(pts[idx].y), label: pts[idx].x }], hit);
        e.preventDefault();
      }
    });
    const off = () => { hideTip(); cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); };
    hit.addEventListener("pointerleave", off);
    hit.addEventListener("blur", off);
    tableView(el, [spec.xName || "Date", spec.yName || "Value"], pts.map((p) => [p.x, f(p.y)]));
  }

  // ---------------------------------------------------------------- stacked status bar (part-to-whole)
  const STATUS = {
    ok: { color: "var(--status-good)", icon: "✓", label: "OK" },
    warning: { color: "var(--status-warning)", icon: "!", label: "Check" },
    issue: { color: "var(--status-critical)", icon: "✕", label: "Issue" },
  };
  function stacked(el, spec) {
    const rows = (spec.rows || []).filter((r) => r.segments.some((sg) => sg.value > 0));
    if (!rows.length) return empty(el, spec.empty);
    const band = 44, thick = 18;
    const { svg, width } = frame(el, rows.length * band + 4, spec);
    const labelW = Math.min(width * 0.35, Math.max(...rows.map((r) => textWidth(svg, r.label, "viz-label"))) + 10);
    const plotW = width - labelW - 8;
    rows.forEach((r, ri) => {
      const y = ri * band + (band - thick) / 2;
      const total = r.segments.reduce((a, sg) => a + sg.value, 0);
      const t = s("text", { x: labelW - 8, y: y + thick / 2, "text-anchor": "end", "dominant-baseline": "middle", class: "viz-label" }, svg);
      t.textContent = fitText(svg, r.label, "viz-label", labelW - 10);
      const segs = r.segments.filter((sg) => sg.value > 0);
      const gaps = 2 * (segs.length - 1);
      let x = labelW;
      segs.forEach((sg, i) => {
        const w = ((plotW - gaps) * sg.value) / total;
        const st = STATUS[sg.status];
        const isFirst = i === 0, isLast = i === segs.length - 1;
        const r4 = 4;
        let d;
        if (isFirst && isLast) d = `M${x + r4},${y}H${x + w - r4}Q${x + w},${y} ${x + w},${y + r4}V${y + thick - r4}Q${x + w},${y + thick} ${x + w - r4},${y + thick}H${x + r4}Q${x},${y + thick} ${x},${y + thick - r4}V${y + r4}Q${x},${y} ${x + r4},${y}Z`;
        else if (isFirst) d = `M${x + r4},${y}H${x + w}V${y + thick}H${x + r4}Q${x},${y + thick} ${x},${y + thick - r4}V${y + r4}Q${x},${y} ${x + r4},${y}Z`;
        else if (isLast) d = barPathH(x, y, w, thick);
        else d = `M${x},${y}H${x + w}V${y + thick}H${x}Z`;
        const mark = s("path", { d, fill: st.color, class: "viz-mark" }, svg);
        const lab = `${st.icon} ${sg.value}`;
        if (textWidth(svg, lab, "viz-inlabel") + 10 < w) {
          const il = s("text", { x: x + w / 2, y: y + thick / 2, "text-anchor": "middle", "dominant-baseline": "middle", class: "viz-inlabel" }, svg);
          il.textContent = lab;
        }
        const hit = s("rect", { x, y: y - 6, width: Math.max(w, 6), height: thick + 12, class: "viz-hit" }, svg);
        hover(hit, () => [{ value: `${sg.value} item${sg.value === 1 ? "" : "s"}`, label: `${st.icon} ${sg.label} · ${r.label}`, color: st.color }], mark);
        x += w + 2;
      });
    });
    legend(el, ["ok", "warning", "issue"].map((k) => ({ label: STATUS[k].label, color: STATUS[k].color, icon: STATUS[k].icon })));
    tableView(el, ["Check", "Result", "Items"], rows.flatMap((r) => r.segments.filter((sg) => sg.value > 0).map((sg) => [r.label, sg.label, sg.value])));
  }

  // ---------------------------------------------------------------- bullet (budget track, spent fill, committed tick)
  function bullet(el, spec) {
    const rows = spec.rows || [];
    if (!rows.length) return empty(el, spec.empty);
    const band = 40, thick = 12;
    const { svg, width } = frame(el, rows.length * band + 8, spec);
    const labelW = Math.min(width * 0.36, Math.max(...rows.map((r) => textWidth(svg, r.label, "viz-label"))) + 10);
    const valueW = 118;
    const plotW = Math.max(60, width - labelW - valueW);
    const max = niceMax(Math.max(...rows.map((r) => Math.max(r.budget || 0, r.committed || 0, r.spent || 0))));
    rows.forEach((r, i) => {
      const y = 4 + i * band + (band - thick) / 2;
      const X = (v) => labelW + (v / max) * plotW;
      const t = s("text", { x: labelW - 8, y: y + thick / 2, "text-anchor": "end", "dominant-baseline": "middle", class: "viz-label" }, svg);
      t.textContent = fitText(svg, r.label, "viz-label", labelW - 10);
      s("path", { d: barPathH(labelW, y, X(r.budget || 0) - labelW, thick), fill: "var(--series-1-track)" }, svg);
      const mark = s("path", { d: barPathH(labelW, y + 3, X(r.spent || 0) - labelW, thick - 6), fill: "var(--series-1)", class: "viz-mark" }, svg);
      if (r.committed !== null && r.committed !== undefined) {
        s("line", { x1: X(r.committed), x2: X(r.committed), y1: y - 4, y2: y + thick + 4, class: "viz-tickmark" }, svg);
      }
      const over = r.committed > r.budget;
      const note = s("text", { x: labelW + plotW + 8, y: y + thick / 2, "dominant-baseline": "middle", class: over ? "viz-value viz-critical" : "viz-value" }, svg);
      note.textContent = over ? "✕ Over budget" : `${Math.round((100 * (r.spent || 0)) / (r.budget || 1))}% spent`;
      const hit = s("rect", { x: 0, y: y - 12, width, height: band, class: "viz-hit" }, svg);
      hover(hit, () => [
        { value: fmt.money(r.spent), label: "Spent" }, { value: fmt.money(r.committed), label: "Committed" },
        { value: fmt.money(r.budget), label: "Budget · " + r.label },
      ], mark);
    });
    legend(el, [{ label: "Spent", color: "var(--series-1)" }, { label: "Budget", color: "var(--series-1-track)" }, { label: "Committed (tick)", color: "var(--viz-ink)", line: true }]);
    tableView(el, ["Item", "Budget", "Committed", "Spent"], rows.map((r) => [r.label, fmt.money(r.budget), fmt.money(r.committed), fmt.money(r.spent)]));
  }

  // ---------------------------------------------------------------- auto chart for query results
  const isNum = (v) => typeof v === "number" && isFinite(v);
  const isDate = (v) => typeof v === "string" && /^\d{4}-\d{2}(-\d{2})?/.test(v);
  function auto(el, columns, rows) {
    if (!rows || rows.length < 2 || rows.length > 30 || !columns || columns.length < 2) return false;
    const numCols = columns.filter((c) => rows.every((r) => r[c] === null || isNum(r[c])) && rows.some((r) => isNum(r[c])));
    const labCol = columns.find((c) => !numCols.includes(c));
    if (!numCols.length || !labCol) return false;
    const valCol = numCols.find((c) => /total|amount|balance|spend|spent|value|revenue|cost|sum|count/i.test(c)) || numCols[numCols.length - 1];
    const money = /total|amount|balance|spend|spent|revenue|cost|price|paid|due/i.test(valCol);
    const f = money ? fmt.money2 : fmt.num;
    if (rows.every((r) => isDate(r[labCol]))) {
      line(el, { points: rows.map((r) => ({ x: r[labCol], y: r[valCol] })).sort((a, b) => (a.x < b.x ? -1 : 1)), format: f, tickFormat: money ? fmt.compact : fmt.num, capFormat: money ? fmt.compact : fmt.num, xName: labCol, yName: valCol });
    } else {
      barH(el, { rows: rows.map((r) => ({ label: String(r[labCol] ?? "—"), value: r[valCol] })).sort((a, b) => b.value - a.value), format: money ? fmt.compact : fmt.num, tipFormat: f, labelName: labCol, valueName: valCol });
    }
    return true;
  }

  window.Charts = { barH, columns, line, stacked, bullet, auto, fmt };
})();

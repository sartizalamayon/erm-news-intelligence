// Export builder (frontend-contract.md §3.4).
//
// Real API — app/api/routers/export.py (CORE-API, live):
//   GET  /export/preview?filter=&topN=&scope=&from=&to=&sections=&sort=&format=
//        -> { rows, summary, filename, counts: { topn, approved, matched, starred } }
//   GET  /export/presets -> [{ id, name, note, filter, topN, scope, sections: [], sort, format }]
//   POST /export/presets  { name, note, filter, topN, scope, sections, sort, format }
//   POST /export?<same query params>  -> binary file, downloaded client-side.
//     Both format=xlsx (openpyxl) and format=csv (stdlib csv) are real
//     writers on the backend (app/services/export.py). EXPORT-XLSX-
//     DEFAULT: xlsx is the default here and on the backend — the owner's
//     explicit call ("export should have .xlsx mainly, not csv") — csv
//     stays available as an explicit second choice.
//
// Column selection AND ordering is real end to end: `columns` (a
// comma-separated, order-preserved list of COLUMNS[].k) is sent on both
// GET /export/preview and POST /export, resolved server-side by
// app.services.export.resolve_columns, and honored by both writers
// (tests/test_core_export.py: test_export_columns_selection_and_order_
// honored_in_{csv,xlsx}). The "4 · Columns" picker drives the actual
// download, not just the local preview chip list.
//
// The prototype's two custom-date-range inputs are inert (no handler at
// all) per §3.4/§5.2 #6-7 — wired for real here since a real export
// needs to accept an arbitrary range.

import { API_BASE } from "../config.js";
import { api, ApiError } from "../api.js";
import { escapeHtml, debounce, RISK_SECTIONS, fmtErr, notWiredBanner } from "../util.js";

const FILTERS = [
  { k: "topn", l: "Top N by relevance", n: "Highest-scoring matched articles, reviewed or not. Best for a weekly digest." },
  { k: "approved", l: "Approved only", n: "Exactly what you signed off. This is what the daily brief uses." },
  { k: "matched", l: "Everything keyword-matched", n: "The full phase-2 pool, before any scoring or review." },
  { k: "starred", l: "Starred", n: "Stories you flagged to come back to." },
];
const SCOPES = [
  { k: "today", l: "Today" },
  { k: "yesterday", l: "Yesterday" },
  { k: "week", l: "Last 7 days" },
  { k: "month", l: "This month" },
  { k: "range", l: "Custom range" },
];
const COLUMNS = [
  { k: "headline", l: "Headline", n: "plus English translation" },
  { k: "source", l: "Source", n: "outlet name" },
  { k: "section", l: "Section", n: "risk category" },
  { k: "keywords", l: "Keywords hit", n: "which terms matched" },
  { k: "published", l: "Published", n: "date and time" },
  { k: "score", l: "Relevance", n: "score and colour band" },
  { k: "reason", l: "Reason", n: "why it scored that" },
  { k: "alsoIn", l: "Also reported by", n: "other outlets" },
  { k: "link", l: "Link", n: "clickable URL" },
  { k: "body", l: "Full article text", n: "makes a heavy file" },
];
const SORTS = [
  { k: "relevance", l: "Relevance" },
  { k: "section", l: "Section" },
  { k: "source", l: "Source" },
  { k: "date", l: "Date" },
];
const FORMATS = [
  { k: "xlsx", l: "Excel .xlsx" },
  { k: "csv", l: "CSV" },
];

export function mountExport(root) {
  let alive = true;
  const s = {
    presetNaming: false, // inline "name this preset" field, replaces window.prompt
    filter: "topn",
    topN: 25,
    scope: "today",
    rangeFrom: "",
    rangeTo: "",
    sections: [...RISK_SECTIONS],
    cols: { headline: 1, source: 1, section: 1, keywords: 1, published: 1, score: 1, reason: 0, alsoIn: 1, link: 1, body: 0 },
    sort: "relevance",
    format: "xlsx", // EXPORT-XLSX-DEFAULT: xlsx is the primary format now (owner's call); csv stays a choice
    preview: null,
    previewLoading: true,
    previewError: null,
    presets: [],
    presetsError: null,
    note: "",
    exporting: false,
  };

  const debouncedPreview = debounce(loadPreview, 250);

  render();
  loadPreview();
  loadPresets();

  function params() {
    return {
      filter: s.filter,
      topN: s.filter === "topn" ? s.topN : undefined,
      scope: s.scope,
      from: s.scope === "range" ? s.rangeFrom : undefined,
      to: s.scope === "range" ? s.rangeTo : undefined,
      sections: s.sections.join(","),
      columns: COLUMNS.filter((c) => s.cols[c.k]).map((c) => c.k).join(","),
      sort: s.sort,
      format: s.format,
    };
  }

  async function loadPreview() {
    s.previewLoading = true;
    s.previewError = null;
    render();
    try {
      const data = await api.get("/export/preview", { query: params() });
      if (!alive) return;
      s.preview = data;
      s.previewLoading = false;
      render();
    } catch (err) {
      if (!alive) return;
      s.previewLoading = false;
      s.previewError = err;
      render();
    }
  }

  async function loadPresets() {
    try {
      const data = await api.get("/export/presets");
      if (!alive) return;
      s.presets = data || [];
      render();
    } catch (err) {
      if (!alive) return;
      s.presetsError = err;
    }
  }

  function rowsCount() {
    return s.preview && typeof s.preview.rows === "number" ? s.preview.rows : null;
  }

  async function doExport() {
    const rows = rowsCount();
    if (!rows) return;
    s.exporting = true;
    s.note = "Preparing " + rows + " rows…";
    render();
    try {
      const qs = new URLSearchParams();
      for (const [k, v] of Object.entries(params())) if (v !== undefined && v !== "") qs.set(k, v);
      const res = await fetch(API_BASE + "/export?" + qs.toString(), { method: "POST", credentials: "include" });
      if (!res.ok) {
        let detail = "Export failed (" + res.status + ")";
        try {
          const body = await res.json();
          if (body && body.detail) detail = body.detail;
        } catch (parseErr) {
          /* non-JSON error body — keep the generic message */
        }
        throw new ApiError(detail, { status: res.status });
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = (s.preview && s.preview.filename) || "export." + s.format;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      s.note = "✓ Downloaded";
    } catch (err) {
      s.note = err && err.kind === "not_found" ? "Export endpoint isn't implemented yet." : "Export failed: " + fmtErr(err);
    }
    s.exporting = false;
    render();
  }

  /**
   * Save the current selections as a named preset.
   *
   * The name used to come from window.prompt(). An unstyled modal that
   * some embeds suppress outright is a poor fit for the one control on
   * this screen that creates something persistent, and the screen
   * already has an inline note slot for every other outcome.
   */
  async function savePreset(name) {
    const trimmed = String(name ?? "").trim();
    if (!trimmed) {
      s.note = "Give the preset a name first.";
      return render();
    }
    try {
      await api.post("/export/presets", { name: trimmed, ...params() });
      s.note = "Saved as a preset.";
      s.presetNaming = false;
      loadPresets();
    } catch (err) {
      s.note = "Couldn't save preset: " + fmtErr(err);
    }
    render();
  }

  function loadPreset(p) {
    s.filter = p.filter || s.filter;
    s.topN = p.topN || s.topN;
    s.scope = p.scope || s.scope;
    s.sections = p.sections ? (Array.isArray(p.sections) ? p.sections : String(p.sections).split(",")) : s.sections;
    if (p.columns) {
      const cols = Array.isArray(p.columns) ? p.columns : String(p.columns).split(",");
      const next = {};
      for (const c of COLUMNS) next[c.k] = cols.includes(c.k) ? 1 : 0;
      s.cols = next;
    }
    s.sort = p.sort || s.sort;
    s.format = p.format || s.format;
    s.note = "✓ Loaded “" + p.name + "”";
    loadPreview();
  }

  function render() {
    const colPreview = COLUMNS.filter((c) => s.cols[c.k]).map((c) => c.l);
    const rows = rowsCount();

    root.innerHTML = `
      <div class="page">
        <div class="export-grid">
          <div class="stack">
            <div class="card">
              <h3>1 · What to include</h3>
              <p class="hint">Pick the set of stories. "Top N by relevance" takes the highest-scoring matched articles whether or not you reviewed them.</p>
              <div class="stack" style="gap:9px;">
                ${FILTERS.map((f) => {
                  const active = s.filter === f.k;
                  const count = s.preview && s.preview.counts ? s.preview.counts[f.k] : "n/a";
                  return `<button class="filter-opt ${active ? "active" : ""}" data-filter="${f.k}">
                    <span class="radio-dot-outer" style="${active ? "border-color:var(--ac2);" : ""}"><span class="radio-dot-inner" style="${active ? "background:var(--ac2);" : ""}"></span></span>
                    <span style="flex:1;min-width:0;">
                      <span style="display:block;font-size:13.5px;font-weight:600;color:var(--tx);">${f.l}</span>
                      <span style="display:block;font-size:12px;color:var(--tx3);line-height:1.5;margin-top:3px;">${f.n}</span>
                    </span>
                    <span class="mono" style="font-size:11px;font-weight:600;color:var(--tx2);">${count === undefined ? "n/a" : count}</span>
                  </button>`;
                }).join("")}
              </div>
              ${
                s.filter === "topn"
                  ? `<div class="banner banner-info" style="margin-top:16px;margin-bottom:0;flex-direction:column;align-items:stretch;">
                <div class="row-between" style="margin-bottom:12px;"><span style="font-size:13px;font-weight:600;color:var(--tx);">Take the top</span><span class="mono" style="font-size:15px;font-weight:600;color:var(--actx);">${s.topN}</span></div>
                <input type="range" min="5" max="100" step="5" value="${s.topN}" id="topNRange" style="width:100%;accent-color:var(--ac2);" />
                <div class="row-between mono" style="font-size:9.5px;color:var(--tx3);margin-top:6px;"><span>5</span><span>100</span></div>
                <div class="row" style="gap:7px;margin-top:12px;">
                  ${[10, 25, 50, 100]
                    .map(
                      (n) =>
                        `<button class="btn-sm" data-topn-preset="${n}" style="flex:1;text-align:center;justify-content:center;${
                          s.topN === n ? "border-color:var(--ac2);background:var(--ac);color:var(--onac);" : ""
                        }">${n}</button>`
                    )
                    .join("")}
                </div>
              </div>`
                  : ""
              }
            </div>

            <div class="card">
              <h3>2 · Date range</h3>
              <p class="hint">Exports read from the archive, so you can pull any past day without re-collecting.</p>
              <div class="row wrap" style="gap:8px;">
                ${SCOPES.map((sc) => `<button class="chip-toggle ${s.scope === sc.k ? "active" : ""}" data-scope="${sc.k}">${sc.l}</button>`).join("")}
              </div>
              ${
                s.scope === "range"
                  ? `<div class="row" style="gap:12px;margin-top:14px;">
                <input type="date" id="rangeFrom" value="${escapeHtml(s.rangeFrom)}" class="text-input" style="padding:10px 12px;font-size:13px;" />
                <span style="color:var(--tx3);font-size:12px;">to</span>
                <input type="date" id="rangeTo" value="${escapeHtml(s.rangeTo)}" class="text-input" style="padding:10px 12px;font-size:13px;" />
              </div>`
                  : ""
              }
            </div>

            <div class="card">
              <h3 style="margin-bottom:16px;">3 · Sections</h3>
              <div class="row wrap" style="gap:8px;">
                ${RISK_SECTIONS.map((sec) => {
                  const on = s.sections.includes(sec);
                  return `<button class="chip-toggle ${on ? "active" : ""}" data-section="${sec}">
                    <span class="chip-mark ${on ? "active" : ""}">${on ? "&#10003;" : ""}</span>${sec}
                  </button>`;
                }).join("")}
              </div>
            </div>

            <div class="card">
              <h3>4 · Columns</h3>
              <p class="hint">Only what you tick becomes a column in the spreadsheet, in this order.</p>
              <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;">
                ${COLUMNS.map((c) => {
                  const on = !!s.cols[c.k];
                  return `<label class="col-check ${on ? "active" : ""}">
                    <input type="checkbox" data-col="${c.k}" ${on ? "checked" : ""} style="accent-color:var(--ac2);width:15px;height:15px;" />
                    <span style="flex:1;min-width:0;">
                      <span style="display:block;font-size:12.5px;font-weight:500;color:var(--tx);">${c.l}</span>
                      <span style="display:block;font-size:10.5px;color:var(--tx3);margin-top:1px;">${c.n}</span>
                    </span>
                  </label>`;
                }).join("")}
              </div>
            </div>

            <div class="card">
              <h3 style="margin-bottom:16px;">5 · Order and format</h3>
              <div class="row wrap" style="gap:22px;">
                <div>
                  <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:8px;">Sort rows by</div>
                  <div class="seg-group">${SORTS.map((o) => `<button class="seg-btn ${s.sort === o.k ? "active" : ""}" data-sort="${o.k}">${o.l}</button>`).join("")}</div>
                </div>
                <div>
                  <div class="mono" style="font-size:9px;letter-spacing:1.3px;text-transform:uppercase;color:var(--tx3);margin-bottom:8px;">File</div>
                  <div class="seg-group">${FORMATS.map((o) => `<button class="seg-btn ${s.format === o.k ? "active" : ""}" data-format="${o.k}">${o.l}</button>`).join("")}</div>
                </div>
              </div>
            </div>
          </div>

          <div class="export-side">
            <div class="card">
              <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);margin-bottom:12px;">Your export</div>
              ${
                s.previewError
                  ? s.previewError.kind === "not_found"
                    ? notWiredBanner("GET /export/preview")
                    : `<div class="banner banner-error" style="margin:0;"><span>&#10005;</span><div>${escapeHtml(fmtErr(s.previewError))}</div></div>`
                  : `<div class="row" style="align-items:baseline;gap:8px;">
                    <div style="font-size:38px;font-weight:700;letter-spacing:-1.6px;line-height:1;">${
                      s.previewLoading ? "…" : rows !== null ? rows.toLocaleString() : "n/a"
                    }</div>
                    <div style="font-size:13px;color:var(--tx2);">rows</div>
                  </div>
                  <div style="font-size:12.5px;color:var(--tx3);line-height:1.55;margin-top:8px;">${escapeHtml((s.preview && s.preview.summary) || "")}</div>`
              }
              <div style="display:flex;flex-direction:column;gap:1px;margin:18px 0;padding:14px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line);">
                ${recapRows()}
              </div>
              <div class="mono" style="font-size:11px;color:var(--tx3);background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 11px;word-break:break-all;">${escapeHtml(
                (s.preview && s.preview.filename) || "erm_export." + s.format
              )}</div>
              <button id="doExportBtn" class="btn btn-primary" style="width:100%;margin-top:14px;padding:13px 20px;font-size:14px;" ${
                !rows || s.exporting ? "disabled" : ""
              }>&#8595; ${s.exporting ? "Preparing…" : "Download " + (s.format === "xlsx" ? "Excel" : "CSV")}</button>
              ${
                s.presetNaming
                  ? `<div class="row" style="gap:6px;margin-top:8px;">
                      <input id="presetNameInput" class="text-input" style="flex:1;padding:9px 11px;font-size:12.5px;" placeholder="Preset name" aria-label="Preset name" />
                      <button id="presetSaveBtn" class="btn btn-primary" style="padding:9px 14px;font-size:12.5px;">Save</button>
                      <button id="presetCancelBtn" class="btn btn-outline" style="padding:9px 12px;font-size:12.5px;">Cancel</button>
                    </div>`
                  : `<button id="savePresetBtn" class="btn btn-outline" style="width:100%;margin-top:8px;padding:11px 20px;font-size:13px;">Save as a preset</button>`
              }
              ${s.note ? `<div style="margin-top:12px;font-size:12px;color:var(--actx);text-align:center;">${escapeHtml(s.note)}</div>` : ""}
            </div>

            <div class="card">
              <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);margin-bottom:12px;">Saved presets</div>
              <div class="stack" style="gap:7px;">
                ${
                  s.presetsError
                    ? s.presetsError.kind === "not_found"
                      ? `<div style="font-size:12px;color:var(--tx3);">Presets aren't implemented on the API yet.</div>`
                      : `<div style="font-size:12px;color:var(--red);">${escapeHtml(fmtErr(s.presetsError))}</div>`
                    : s.presets.length
                    ? s.presets
                        .map(
                          (p, i) => `<button class="preset-btn" data-preset="${i}">
                    <span style="flex:1;min-width:0;">
                      <span style="display:block;font-size:12.5px;font-weight:600;color:var(--tx);">${escapeHtml(p.name)}</span>
                      <span style="display:block;font-size:11px;color:var(--tx3);margin-top:2px;">${escapeHtml(p.note || "")}</span>
                    </span>
                    <span class="mono" style="flex:0 0 auto;font-size:10px;color:var(--actx);">load</span>
                  </button>`
                        )
                        .join("")
                    : `<div style="font-size:12px;color:var(--tx3);">No saved presets yet.</div>`
                }
              </div>
            </div>

            <div class="card">
              <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);margin-bottom:12px;">Column preview</div>
              <div class="row wrap" style="gap:6px;">
                ${colPreview.map((c) => `<span class="mono" style="font-size:10.5px;color:var(--actx);background:var(--acsoft);padding:3px 9px;border-radius:5px;">${escapeHtml(c)}</span>`).join("") || `<span style="font-size:12px;color:var(--tx3);">No columns selected.</span>`}
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
    bind();
  }

  function recapRows() {
    const rows = [
      ["Set", FILTERS.find((f) => f.k === s.filter)?.l || s.filter],
      ["Dates", SCOPES.find((sc) => sc.k === s.scope)?.l || s.scope],
      ["Sections", s.sections.length === 5 ? "All five" : s.sections.join(", ") || "none selected"],
      ["Columns", Object.values(s.cols).filter(Boolean).length + " of 10"],
      ["Sorted", "by " + s.sort],
    ];
    return rows
      .map(([k, v]) => `<div class="recap-row"><span class="recap-k mono">${escapeHtml(k)}</span><span style="flex:1;font-size:12.5px;color:var(--tx2);">${escapeHtml(v)}</span></div>`)
      .join("");
  }

  function bind() {
    root.querySelectorAll("[data-filter]").forEach((b) => b.addEventListener("click", () => { s.filter = b.getAttribute("data-filter"); debouncedPreview(); render(); }));
    const topNRange = root.querySelector("#topNRange");
    if (topNRange) topNRange.addEventListener("input", (e) => { s.topN = Number(e.target.value); debouncedPreview(); });
    root.querySelectorAll("[data-topn-preset]").forEach((b) => b.addEventListener("click", () => { s.topN = Number(b.getAttribute("data-topn-preset")); debouncedPreview(); render(); }));
    root.querySelectorAll("[data-scope]").forEach((b) => b.addEventListener("click", () => { s.scope = b.getAttribute("data-scope"); debouncedPreview(); render(); }));
    const rangeFrom = root.querySelector("#rangeFrom");
    if (rangeFrom) rangeFrom.addEventListener("change", (e) => { s.rangeFrom = e.target.value; debouncedPreview(); });
    const rangeTo = root.querySelector("#rangeTo");
    if (rangeTo) rangeTo.addEventListener("change", (e) => { s.rangeTo = e.target.value; debouncedPreview(); });
    root.querySelectorAll("[data-section]").forEach((b) =>
      b.addEventListener("click", () => {
        const sec = b.getAttribute("data-section");
        s.sections = s.sections.includes(sec) ? s.sections.filter((x) => x !== sec) : s.sections.concat([sec]);
        debouncedPreview();
        render();
      })
    );
    root.querySelectorAll("[data-col]").forEach((cb) =>
      cb.addEventListener("change", () => {
        s.cols[cb.getAttribute("data-col")] = cb.checked ? 1 : 0;
        render();
      })
    );
    root.querySelectorAll("[data-sort]").forEach((b) => b.addEventListener("click", () => { s.sort = b.getAttribute("data-sort"); debouncedPreview(); render(); }));
    root.querySelectorAll("[data-format]").forEach((b) => b.addEventListener("click", () => { s.format = b.getAttribute("data-format"); render(); }));

    const doExportBtn = root.querySelector("#doExportBtn");
    if (doExportBtn) doExportBtn.addEventListener("click", doExport);
    const savePresetBtn = root.querySelector("#savePresetBtn");
    if (savePresetBtn)
      savePresetBtn.addEventListener("click", () => {
        s.presetNaming = true;
        render();
        root.querySelector("#presetNameInput")?.focus();
      });
    const presetNameInput = root.querySelector("#presetNameInput");
    if (presetNameInput)
      presetNameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") savePreset(e.target.value);
        if (e.key === "Escape") {
          s.presetNaming = false;
          render();
        }
      });
    const presetSaveBtn = root.querySelector("#presetSaveBtn");
    if (presetSaveBtn)
      presetSaveBtn.addEventListener("click", () => savePreset(root.querySelector("#presetNameInput").value));
    const presetCancelBtn = root.querySelector("#presetCancelBtn");
    if (presetCancelBtn)
      presetCancelBtn.addEventListener("click", () => {
        s.presetNaming = false;
        render();
      });
    root.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => loadPreset(s.presets[Number(b.getAttribute("data-preset"))])));
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

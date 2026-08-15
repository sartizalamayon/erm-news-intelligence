// Keywords admin (frontend-contract.md §3.8).
//
// Real API — app/api/routers/keywords.py (ADMIN-API, landed; extended
// by KEYWORDS-CONFIG for custom sections + xlsx import/export):
//   GET    /keywords?section=       -> KeywordOut[] (id,section,term_en,term_bn,enabled,created_at)
//   GET    /keywords/sections       -> [{ section, count }]
//   PATCH  /keywords/sections/{s}   { name }   -> [{ section, count }]  (bulk-rename)
//   GET    /keywords/export.xlsx    -> binary .xlsx (all keywords)
//   POST   /keywords/import         <raw .xlsx bytes as body>  -> { created, updated, skipped, errors[] }
//   POST   /keywords                { section?, term_en?, term_bn?, enabled } -> KeywordOut
//   PATCH  /keywords/{id}           (partial)                              -> KeywordOut
//   DELETE /keywords/{id}           -> 204
//   POST   /keywords/{id}/toggle    -> KeywordOut
//   POST   /keywords/rematch        -> { matched, filtered_out, total }
//
// KEYWORDS-CONFIG additions over the original ADMIN-API surface:
//   - `section` is fully free-form (no fixed list anywhere at the DB or
//     API layer) — the Add-keyword form below lets an admin type a
//     brand new section name instead of only picking from what already
//     exists. The section chips row IS the "browse sections in use" UI
//     (derived from distinct values via GET /keywords/sections, same as
//     before); a small "rename" control next to it is the "manage" half.
//   - `section` can be entirely absent — such keywords come back from
//     the API under the literal section "Uncategorized" (never `null`;
//     see KeywordOut's docstring in app/api/schemas/admin.py).
//   - `term_en`/`term_bn` are independently optional: a keyword can be
//     Bangla-only, English-only, or both. Enforced below by requiring
//     at least one of the two, same as before this mission (that check
//     already existed here) — what changed is the backend
//     (app.services.matching.keyword_rows_to_groups) now actually
//     matches a Bangla-only keyword instead of silently compiling it to
//     zero patterns.
//
// `term_bn` can be "" for English-only terms (§3.8: "treat as a real
// 'no Bengali equivalent' state, not missing data") — rendered as an
// em-dash, matching the prototype's own "—" placeholder.

import { API_BASE } from "../config.js";
import { api, ApiError } from "../api.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows } from "../util.js";

const NEW_SECTION_OPTION = "__new_section__";
const UNCATEGORIZED_OPTION = "";

export function mountKeywords(root) {
  let alive = true;
  const s = {
    sections: [],
    sectionsError: null,
    activeSection: null,
    rows: [],
    loading: true,
    error: null,
    addingOpen: false,
    addFormSectionMode: "existing", // "existing" | "new"
    rematching: false,
    rematchNote: "",
    exporting: false,
    importing: false,
    ioNote: "",
  };

  render();
  loadSections();

  async function loadSections() {
    try {
      const data = await api.get("/keywords/sections");
      if (!alive) return;
      s.sections = data || [];
      s.sectionsError = null;
      if (!s.activeSection && s.sections.length) s.activeSection = s.sections[0].section;
      render();
      if (s.activeSection) loadRows();
    } catch (err) {
      if (!alive) return;
      s.sectionsError = err;
      s.loading = false;
      render();
    }
  }

  async function loadRows() {
    s.loading = true;
    s.error = null;
    render();
    try {
      const data = await api.get("/keywords", { query: { section: s.activeSection } });
      if (!alive) return;
      s.rows = data || [];
      s.loading = false;
      render();
    } catch (err) {
      if (!alive) return;
      s.loading = false;
      s.error = err;
      render();
    }
  }

  async function toggle(row) {
    const prev = row.enabled;
    row.enabled = !prev;
    render();
    try {
      const updated = await api.post(`/keywords/${row.id}/toggle`);
      Object.assign(row, updated);
      render();
    } catch (err) {
      row.enabled = prev;
      render();
    }
  }

  async function remove(row) {
    if (!window.confirm(`Delete "${row.term_en || row.term_bn}"?`)) return;
    try {
      await api.delete(`/keywords/${row.id}`);
      loadRows();
      loadSections();
    } catch (err) {
      window.alert("Couldn't delete: " + fmtErr(err));
    }
  }

  async function rematch() {
    s.rematching = true;
    s.rematchNote = "";
    render();
    try {
      const res = await api.post("/keywords/rematch");
      s.rematchNote = `✓ ${res.matched} matched, ${res.filtered_out} filtered out (of ${res.total} candidates)`;
    } catch (err) {
      s.rematchNote = err.kind === "not_found" ? "Rematch endpoint not available." : "Rematch failed: " + fmtErr(err);
    }
    s.rematching = false;
    render();
  }

  async function renameActiveSection() {
    if (!s.activeSection) return;
    const next = window.prompt(`Rename section "${s.activeSection}" to:`, s.activeSection);
    if (next === null) return;
    const trimmed = next.trim();
    if (trimmed === s.activeSection) return;
    try {
      const sections = await api.patch(`/keywords/sections/${encodeURIComponent(s.activeSection)}`, { name: trimmed });
      s.sections = sections || [];
      s.activeSection = trimmed === "" ? "Uncategorized" : trimmed;
      if (!s.sections.some((t) => t.section === s.activeSection)) s.activeSection = s.sections[0]?.section || null;
      loadRows();
    } catch (err) {
      window.alert("Couldn't rename section: " + fmtErr(err));
    }
  }

  async function exportXlsx() {
    s.exporting = true;
    s.ioNote = "";
    render();
    try {
      const res = await fetch(API_BASE + "/keywords/export.xlsx", { credentials: "include" });
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
      a.download = "keywords.xlsx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      s.ioNote = "✓ Exported";
    } catch (err) {
      s.ioNote = err && err.kind === "not_found" ? "Export endpoint isn't implemented yet." : "Export failed: " + fmtErr(err);
    }
    s.exporting = false;
    render();
  }

  async function importXlsx(file) {
    if (!file) return;
    s.importing = true;
    s.ioNote = "Importing " + file.name + "…";
    render();
    try {
      const res = await fetch(API_BASE + "/keywords/import", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/octet-stream" },
        body: file,
      });
      const text = await res.text();
      let payload = null;
      try {
        payload = text ? JSON.parse(text) : null;
      } catch (parseErr) {
        payload = null;
      }
      if (!res.ok) {
        const detail = (payload && payload.detail) || "Import failed (" + res.status + ")";
        throw new ApiError(detail, { status: res.status });
      }
      const r = payload || {};
      const errCount = (r.errors || []).length;
      s.ioNote = `✓ Import done: ${r.created || 0} created, ${r.updated || 0} updated, ${r.skipped || 0} skipped${
        errCount ? `, ${errCount} row error(s): ${r.errors.slice(0, 3).join("; ")}${errCount > 3 ? "…" : ""}` : ""
      }`;
      loadSections();
      loadRows();
    } catch (err) {
      s.ioNote = err && err.kind === "not_found" ? "Import endpoint isn't implemented yet." : "Import failed: " + fmtErr(err);
    }
    s.importing = false;
    render();
  }

  function render() {
    root.innerHTML = `
      <div class="page page-md" style="max-width:1080px;">
        <div class="row-between wrap" style="margin-bottom:20px;">
          <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0;max-width:58ch;">Bilingual search terms across risk sections, the whole of phase 2. Changes apply to the next collection, or re-run the filter to re-check today's articles.</p>
          <div class="row wrap" style="gap:9px;">
            <input type="file" id="kwImportFile" accept=".xlsx" style="display:none;" />
            <button id="kwImportBtn" class="btn btn-outline" style="padding:11px 16px;white-space:nowrap;" ${s.importing ? "disabled" : ""}>&#8593; ${
      s.importing ? "Importing…" : "Import .xlsx"
    }</button>
            <button id="kwExportBtn" class="btn btn-outline" style="padding:11px 16px;white-space:nowrap;" ${s.exporting ? "disabled" : ""}>&#8595; ${
      s.exporting ? "Exporting…" : "Export .xlsx"
    }</button>
            <button id="rematchBtn" class="btn btn-outline" style="padding:11px 16px;white-space:nowrap;" ${s.rematching ? "disabled" : ""}>&#8635; ${
      s.rematching ? "Rematching…" : "Rematch today"
    }</button>
            <button id="addKeywordBtn" class="btn btn-primary" style="padding:11px 18px;white-space:nowrap;">+ Add keyword</button>
          </div>
        </div>

        ${s.rematchNote ? `<div class="banner banner-info">${escapeHtml(s.rematchNote)}</div>` : ""}
        ${s.ioNote ? `<div class="banner banner-info">${escapeHtml(s.ioNote)}</div>` : ""}

        ${
          s.sectionsError
            ? s.sectionsError.kind === "forbidden"
              ? `<div class="banner banner-error"><span>&#128274;</span><div>Keywords are admin-only. Your account doesn't have the admin role.</div></div>`
              : notWiredBanner("GET /keywords/sections")
            : `<div class="row wrap" style="gap:8px;margin-bottom:10px;align-items:center;">
              ${s.sections
                .map(
                  (t) => `<button class="chip-toggle ${s.activeSection === t.section ? "active" : ""}" data-section="${escapeHtml(t.section)}">
                ${escapeHtml(t.section)} <span class="mono" style="font-size:10.5px;">${t.count}</span>
              </button>`
                )
                .join("")}
              ${
                s.activeSection
                  ? `<button id="kwRenameSectionBtn" class="btn-sm" title="Rename this section">rename "${escapeHtml(s.activeSection)}"</button>`
                  : ""
              }
            </div>`
        }

        ${s.addingOpen ? keywordForm() : ""}

        ${
          !s.sectionsError
            ? `<div class="table-wrap">
              <div class="table-head-row keywords-cols">
                <div class="table-th">English</div><div class="table-th">বাংলা</div><div class="table-th">Status</div><div class="table-th" style="text-align:right;">Actions</div>
              </div>
              ${
                s.loading
                  ? Array.from({ length: 6 })
                      .map(() => `<div class="table-row keywords-cols">${skeletonRows(1).repeat(4)}</div>`)
                      .join("")
                  : s.error
                  ? s.error.kind === "not_found"
                    ? `<div style="padding:16px 22px;">${notWiredBanner("GET /keywords")}</div>`
                    : `<div style="padding:16px 22px;"><div class="banner banner-error"><span>&#10005;</span><div>${escapeHtml(fmtErr(s.error))}</div></div></div>`
                  : s.rows.length
                  ? s.rows.map((k) => keywordRow(k)).join("")
                  : `<div class="state-block"><div class="state-title">No keywords in this section</div></div>`
              }
            </div>`
            : ""
        }
      </div>
    `;
    bind();
  }

  function keywordRow(k) {
    const on = !!k.enabled;
    return `<div class="table-row keywords-cols" style="opacity:${on ? 1 : 0.55}">
      <div style="font-size:13.5px;font-weight:600;">${escapeHtml(k.term_en || "n/a")}</div>
      <div style="font-size:13.5px;color:var(--tx2);">${escapeHtml(k.term_bn || "n/a")}</div>
      <div><span class="pill" style="background:${on ? "var(--acsoft)" : "var(--gridline)"};color:${on ? "var(--actx)" : "var(--tx3)"}"><span class="pill-dot" style="background:${
      on ? "var(--ac2)" : "var(--tx4)"
    }"></span>${on ? "on" : "off"}</span></div>
      <div class="row" style="gap:6px;justify-content:flex-end;">
        <button class="btn-sm" data-toggle="${k.id}">${on ? "disable" : "enable"}</button>
        <button class="btn-sm btn-sm-danger" data-delete="${k.id}">delete</button>
      </div>
    </div>`;
  }

  function keywordForm() {
    const newMode = s.addFormSectionMode === "new";
    return `<div class="card" style="margin-bottom:16px;">
      <h3>Add keyword</h3>
      <div class="stack" style="gap:10px;max-width:520px;">
        <div>
          <label class="field-label">Section</label>
          ${
            newMode
              ? `<input id="kwFormNewSection" class="text-input" style="padding:9px 12px;font-size:13px;" placeholder="New section name" />
                 <button id="kwFormSectionBack" class="btn-sm" style="margin-top:6px;">&larr; pick an existing section instead</button>`
              : `<select id="kwFormSection" class="text-input" style="padding:9px 12px;font-size:13px;">
                  <option value="${UNCATEGORIZED_OPTION}">No section (Uncategorized)</option>
                  ${s.sections
                    .filter((t) => t.section !== "Uncategorized")
                    .map((t) => `<option value="${escapeHtml(t.section)}" ${t.section === s.activeSection ? "selected" : ""}>${escapeHtml(t.section)}</option>`)
                    .join("")}
                  <option value="${NEW_SECTION_OPTION}">+ New section…</option>
                </select>`
          }
        </div>
        <div>
          <label class="field-label">English term (leave blank if Bangla-only)</label>
          <input id="kwFormEn" class="text-input" style="padding:9px 12px;font-size:13px;" />
        </div>
        <div>
          <label class="field-label">Bengali term (leave blank if English-only)</label>
          <input id="kwFormBn" class="text-input" style="padding:9px 12px;font-size:13px;" />
        </div>
        <div class="row" style="gap:9px;">
          <button id="kwFormSave" class="btn btn-primary" style="padding:9px 16px;">Add keyword</button>
          <button id="kwFormCancel" class="btn btn-outline" style="padding:9px 16px;">Cancel</button>
        </div>
      </div>
    </div>`;
  }

  function bind() {
    root.querySelectorAll("[data-section]").forEach((b) =>
      b.addEventListener("click", () => {
        s.activeSection = b.getAttribute("data-section");
        loadRows();
      })
    );
    const addBtn = root.querySelector("#addKeywordBtn");
    if (addBtn) addBtn.addEventListener("click", () => { s.addingOpen = true; s.addFormSectionMode = "existing"; render(); });
    const rematchBtn = root.querySelector("#rematchBtn");
    if (rematchBtn) rematchBtn.addEventListener("click", rematch);
    const renameBtn = root.querySelector("#kwRenameSectionBtn");
    if (renameBtn) renameBtn.addEventListener("click", renameActiveSection);

    const exportBtn = root.querySelector("#kwExportBtn");
    if (exportBtn) exportBtn.addEventListener("click", exportXlsx);
    const importBtn = root.querySelector("#kwImportBtn");
    const importFile = root.querySelector("#kwImportFile");
    if (importBtn && importFile) {
      importBtn.addEventListener("click", () => importFile.click());
      importFile.addEventListener("change", () => {
        const file = importFile.files && importFile.files[0];
        importFile.value = "";
        if (file) importXlsx(file);
      });
    }

    root.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => toggle(s.rows.find((x) => x.id == b.getAttribute("data-toggle")))));
    root.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => remove(s.rows.find((x) => x.id == b.getAttribute("data-delete")))));

    const sectionSelect = root.querySelector("#kwFormSection");
    if (sectionSelect)
      sectionSelect.addEventListener("change", () => {
        if (sectionSelect.value === NEW_SECTION_OPTION) {
          s.addFormSectionMode = "new";
          render();
        }
      });
    const sectionBack = root.querySelector("#kwFormSectionBack");
    if (sectionBack) sectionBack.addEventListener("click", () => { s.addFormSectionMode = "existing"; render(); });

    const saveBtn = root.querySelector("#kwFormSave");
    if (saveBtn)
      saveBtn.addEventListener("click", async () => {
        const section =
          s.addFormSectionMode === "new"
            ? (root.querySelector("#kwFormNewSection")?.value || "").trim()
            : root.querySelector("#kwFormSection").value;
        const term_en = root.querySelector("#kwFormEn").value.trim();
        const term_bn = root.querySelector("#kwFormBn").value.trim();
        if (!term_en && !term_bn) return window.alert("Enter at least one of English/Bengali.");
        if (s.addFormSectionMode === "new" && !section) return window.alert("Enter a name for the new section.");
        try {
          await api.post("/keywords", { section: section || null, term_en, term_bn, enabled: true });
          s.addingOpen = false;
          s.addFormSectionMode = "existing";
          s.activeSection = section || "Uncategorized";
          loadRows();
          loadSections();
        } catch (err) {
          window.alert("Couldn't save: " + fmtErr(err));
        }
      });
    const cancelBtn = root.querySelector("#kwFormCancel");
    if (cancelBtn) cancelBtn.addEventListener("click", () => { s.addingOpen = false; render(); });
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

// Keywords admin (frontend-contract.md §3.8).
//
// Real API — app/api/routers/keywords.py (ADMIN-API, landed):
//   GET    /keywords?section=       -> KeywordOut[] (id,section,term_en,term_bn,enabled,created_at)
//   GET    /keywords/sections       -> [{ section, count }]
//   POST   /keywords                { section, term_en, term_bn, enabled } -> KeywordOut
//   PATCH  /keywords/{id}           (partial)                              -> KeywordOut
//   DELETE /keywords/{id}           -> 204
//   POST   /keywords/{id}/toggle    -> KeywordOut
//   POST   /keywords/rematch        -> { matched, filtered_out, total }
//
// `term_bn` can be "" for English-only terms (§3.8: "treat as a real
// 'no Bengali equivalent' state, not missing data") — rendered as an
// em-dash, matching the prototype's own "—" placeholder.

import { api } from "../api.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows } from "../util.js";

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
    rematching: false,
    rematchNote: "",
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
      s.rematchNote = err.kind === "not_found" ? "Rematch endpoint not available." : "Rematch failed — " + fmtErr(err);
    }
    s.rematching = false;
    render();
  }

  function render() {
    root.innerHTML = `
      <div class="page page-md" style="max-width:1080px;">
        <div class="row-between wrap" style="margin-bottom:20px;">
          <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0;max-width:58ch;">Bilingual search terms across risk sections — the whole of phase 2. Changes apply to the next collection, or re-run the filter to re-check today's articles.</p>
          <div class="row" style="gap:9px;">
            <button id="rematchBtn" class="btn btn-outline" style="padding:11px 16px;white-space:nowrap;" ${s.rematching ? "disabled" : ""}>&#8635; ${
      s.rematching ? "Rematching…" : "Rematch today"
    }</button>
            <button id="addKeywordBtn" class="btn btn-primary" style="padding:11px 18px;white-space:nowrap;">+ Add keyword</button>
          </div>
        </div>

        ${s.rematchNote ? `<div class="banner banner-info">${escapeHtml(s.rematchNote)}</div>` : ""}

        ${
          s.sectionsError
            ? s.sectionsError.kind === "forbidden"
              ? `<div class="banner banner-error"><span>&#128274;</span><div>Keywords are admin-only. Your account doesn't have the admin role.</div></div>`
              : notWiredBanner("GET /keywords/sections")
            : `<div class="row wrap" style="gap:8px;margin-bottom:18px;">
              ${s.sections
                .map(
                  (t) => `<button class="chip-toggle ${s.activeSection === t.section ? "active" : ""}" data-section="${escapeHtml(t.section)}">
                ${escapeHtml(t.section)} <span class="mono" style="font-size:10.5px;">${t.count}</span>
              </button>`
                )
                .join("")}
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
      <div style="font-size:13.5px;font-weight:600;">${escapeHtml(k.term_en || "—")}</div>
      <div style="font-size:13.5px;color:var(--tx2);">${escapeHtml(k.term_bn || "—")}</div>
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
    return `<div class="card" style="margin-bottom:16px;">
      <h3>Add keyword</h3>
      <div class="stack" style="gap:10px;max-width:520px;">
        <div>
          <label class="field-label">Section</label>
          <select id="kwFormSection" class="text-input" style="padding:9px 12px;font-size:13px;">
            ${s.sections.map((t) => `<option value="${escapeHtml(t.section)}" ${t.section === s.activeSection ? "selected" : ""}>${escapeHtml(t.section)}</option>`).join("")}
          </select>
        </div>
        <div>
          <label class="field-label">English term</label>
          <input id="kwFormEn" class="text-input" style="padding:9px 12px;font-size:13px;" />
        </div>
        <div>
          <label class="field-label">Bengali term (leave blank if none)</label>
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
    if (addBtn) addBtn.addEventListener("click", () => { s.addingOpen = true; render(); });
    const rematchBtn = root.querySelector("#rematchBtn");
    if (rematchBtn) rematchBtn.addEventListener("click", rematch);

    root.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => toggle(s.rows.find((x) => x.id == b.getAttribute("data-toggle")))));
    root.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => remove(s.rows.find((x) => x.id == b.getAttribute("data-delete")))));

    const saveBtn = root.querySelector("#kwFormSave");
    if (saveBtn)
      saveBtn.addEventListener("click", async () => {
        const section = root.querySelector("#kwFormSection").value;
        const term_en = root.querySelector("#kwFormEn").value.trim();
        const term_bn = root.querySelector("#kwFormBn").value.trim();
        if (!term_en && !term_bn) return window.alert("Enter at least one of English/Bengali.");
        try {
          await api.post("/keywords", { section, term_en, term_bn, enabled: true });
          s.addingOpen = false;
          s.activeSection = section;
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

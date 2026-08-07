// Sources admin (frontend-contract.md §3.7).
//
// Real API — app/api/routers/sources.py (ADMIN-API, landed):
//   GET    /sources?enabled=          -> SourceOut[]  (id,name,methods[],language,enabled,config,created_at)
//   POST   /sources                   SourceCreate { name, methods[], language, enabled, config } -> SourceOut
//   PATCH  /sources/{id}              SourceUpdate (all fields optional)   -> SourceOut
//   DELETE /sources/{id}              -> 204
//   POST   /sources/{id}/toggle       -> SourceOut (flips enabled)
//   POST   /sources/{id}/test         -> SourceTestResult { source_id, ok, fetched, today, method, detail }
//
// No pagination on the list endpoint (returns the full set) — the
// prototype's hardcoded "showing 12 of 46" footer (§8 item 16) is
// replaced with a real total count instead of fake pagination controls.
// Every route requires admin (require_admin, app/api/deps.py) — a
// non-admin hitting this screen gets a real 403, handled below, not
// just a hidden nav link.

import { api } from "../api.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows } from "../util.js";

const LANGS = ["en", "bn", "mixed"];

export function mountSources(root) {
  let alive = true;
  const s = {
    items: [],
    loading: true,
    error: null,
    testResult: null,
    testOk: true,
    addingOpen: false,
    editingId: null,
    busyIds: new Set(),
  };

  render();
  load();

  async function load() {
    s.loading = true;
    render();
    try {
      const data = await api.get("/sources");
      if (!alive) return;
      s.items = data || [];
      s.loading = false;
      s.error = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.loading = false;
      s.error = err;
      render();
    }
  }

  async function testSource(src) {
    s.busyIds.add(src.id);
    render();
    try {
      const res = await api.post(`/sources/${src.id}/test`);
      s.testResult = `${src.name} — ${res.fetched} fetched, ${res.today} from today (via ${res.method})${res.detail ? " · " + res.detail : ""}`;
      s.testOk = !!res.ok;
    } catch (err) {
      s.testResult = `${src.name} — ${fmtErr(err)}`;
      s.testOk = false;
    }
    s.busyIds.delete(src.id);
    render();
  }

  async function toggleEnabled(src) {
    const prev = src.enabled;
    src.enabled = !prev; // optimistic
    render();
    try {
      const updated = await api.post(`/sources/${src.id}/toggle`);
      Object.assign(src, updated);
      render();
    } catch (err) {
      src.enabled = prev;
      render();
    }
  }

  async function deleteSource(src) {
    if (!window.confirm(`Delete ${src.name}? This can't be undone.`)) return;
    try {
      await api.delete(`/sources/${src.id}`);
      load();
    } catch (err) {
      window.alert("Couldn't delete: " + fmtErr(err));
    }
  }

  function render() {
    root.innerHTML = `
      <div class="page page-wide">
        <div class="row-between wrap" style="margin-bottom:20px;">
          <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0;max-width:58ch;">The outlets feeding the desk. Collection runs every enabled method per source and merges the results — add, edit, disable or test any source without touching code.</p>
          <button id="addSourceBtn" class="btn btn-primary" style="padding:11px 18px;white-space:nowrap;">+ Add source</button>
        </div>

        ${
          s.testResult
            ? `<div class="banner ${s.testOk ? "banner-ok" : "banner-error"}"><span>${s.testOk ? "&#10003;" : "&#10005;"}</span><div style="flex:1;">${escapeHtml(
                s.testResult
              )}</div><button id="clearTestBtn" class="btn-ghost" style="font-size:14px;">&#10005;</button></div>`
            : ""
        }

        ${s.addingOpen ? sourceForm(null) : ""}

        ${
          s.error
            ? s.error.kind === "forbidden"
              ? `<div class="banner banner-error"><span>&#128274;</span><div>Sources are admin-only. Your account doesn't have the admin role.</div></div>`
              : s.error.kind === "not_found"
              ? notWiredBanner("GET /sources")
              : `<div class="banner banner-error"><span>&#10005;</span><div>${escapeHtml(fmtErr(s.error))}</div></div>`
            : `<div class="table-wrap">
              <div class="table-head-row sources-cols">
                <div class="table-th">Source</div><div class="table-th">Methods</div><div class="table-th">Lang</div><div class="table-th">Status</div><div class="table-th" style="text-align:right;">Actions</div>
              </div>
              ${
                s.loading
                  ? Array.from({ length: 6 })
                      .map(() => `<div class="table-row sources-cols">${skeletonRows(1).repeat(5)}</div>`)
                      .join("")
                  : s.items.length
                  ? s.items.map((src) => (s.editingId === src.id ? sourceForm(src) : sourceRow(src))).join("")
                  : `<div class="state-block"><div class="state-title">No sources configured</div><p class="state-body">Add the first outlet to start collecting.</p></div>`
              }
              ${!s.loading && s.items.length ? `<div style="padding:13px 22px;" class="mono" style="font-size:10.5px;color:var(--tx4);">${s.items.length} source${s.items.length === 1 ? "" : "s"} total</div>` : ""}
            </div>`
        }
      </div>
    `;
    bind();
  }

  function sourceRow(src) {
    const on = !!src.enabled;
    return `<div class="table-row sources-cols" style="opacity:${on ? 1 : 0.55}">
      <div style="font-size:13.5px;font-weight:600;letter-spacing:-.1px;">${escapeHtml(src.name)}</div>
      <div class="row wrap" style="gap:5px;">${(src.methods || []).map((m) => `<span class="mono" style="font-size:10.5px;color:var(--actx);background:var(--acsoft);padding:2px 8px;border-radius:5px;">${escapeHtml(m)}</span>`).join("")}</div>
      <div class="mono" style="font-size:11px;color:var(--tx2);">${escapeHtml(src.language)}</div>
      <div><span class="pill" style="background:${on ? "var(--acsoft)" : "var(--gridline)"};color:${on ? "var(--actx)" : "var(--tx3)"}"><span class="pill-dot" style="background:${
      on ? "var(--ac2)" : "var(--tx4)"
    }"></span>${on ? "on" : "off"}</span></div>
      <div class="row" style="gap:6px;justify-content:flex-end;">
        <button class="btn-sm" data-test="${src.id}" ${s.busyIds.has(src.id) ? "disabled" : ""}>test</button>
        <button class="btn-sm" data-edit="${src.id}">edit</button>
        <button class="btn-sm" data-toggle="${src.id}">${on ? "disable" : "enable"}</button>
        <button class="btn-sm btn-sm-danger" data-delete="${src.id}">delete</button>
      </div>
    </div>`;
  }

  function sourceForm(src) {
    const isEdit = !!src;
    return `<div class="card" style="margin-bottom:16px;">
      <h3>${isEdit ? "Edit source" : "Add source"}</h3>
      <div class="stack" style="gap:10px;max-width:520px;">
        <div>
          <label class="field-label">Name</label>
          <input id="srcFormName" class="text-input" style="padding:9px 12px;font-size:13px;" value="${escapeHtml(src ? src.name : "")}" />
        </div>
        <div>
          <label class="field-label">Methods (comma-separated: sitemap, RSS, API, Google News, ID scan, archive)</label>
          <input id="srcFormMethods" class="text-input" style="padding:9px 12px;font-size:13px;" value="${escapeHtml(src ? (src.methods || []).join(", ") : "")}" />
        </div>
        <div>
          <label class="field-label">Language</label>
          <select id="srcFormLang" class="text-input" style="padding:9px 12px;font-size:13px;">
            ${LANGS.map((l) => `<option value="${l}" ${src && src.language === l ? "selected" : ""}>${l}</option>`).join("")}
          </select>
        </div>
        <div class="row" style="gap:9px;">
          <button id="srcFormSave" class="btn btn-primary" style="padding:9px 16px;">${isEdit ? "Save" : "Add source"}</button>
          <button id="srcFormCancel" class="btn btn-outline" style="padding:9px 16px;">Cancel</button>
        </div>
      </div>
    </div>`;
  }

  function bind() {
    const addBtn = root.querySelector("#addSourceBtn");
    if (addBtn) addBtn.addEventListener("click", () => { s.addingOpen = true; s.editingId = null; render(); });
    const clearTestBtn = root.querySelector("#clearTestBtn");
    if (clearTestBtn) clearTestBtn.addEventListener("click", () => { s.testResult = null; render(); });

    root.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", () => testSource(s.items.find((x) => x.id == b.getAttribute("data-test")))));
    root.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => toggleEnabled(s.items.find((x) => x.id == b.getAttribute("data-toggle")))));
    root.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => deleteSource(s.items.find((x) => x.id == b.getAttribute("data-delete")))));
    root.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => {
        s.editingId = Number(b.getAttribute("data-edit"));
        s.addingOpen = false;
        render();
      })
    );

    const saveBtn = root.querySelector("#srcFormSave");
    if (saveBtn)
      saveBtn.addEventListener("click", async () => {
        const name = root.querySelector("#srcFormName").value.trim();
        const methods = root
          .querySelector("#srcFormMethods")
          .value.split(",")
          .map((x) => x.trim())
          .filter(Boolean);
        const language = root.querySelector("#srcFormLang").value;
        if (!name) return window.alert("Name is required.");
        try {
          if (s.editingId) {
            await api.patch(`/sources/${s.editingId}`, { name, methods, language });
          } else {
            await api.post("/sources", { name, methods, language, enabled: true });
          }
          s.addingOpen = false;
          s.editingId = null;
          load();
        } catch (err) {
          window.alert("Couldn't save: " + fmtErr(err));
        }
      });
    const cancelBtn = root.querySelector("#srcFormCancel");
    if (cancelBtn) cancelBtn.addEventListener("click", () => { s.addingOpen = false; s.editingId = null; render(); });
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

// Sources: the one page for every outlet feeding the desk.
//
// This REPLACES two screens that described the same 51 rows from
// opposite ends. "Sources" showed name / methods / language and let you
// edit them. "Source health" showed the same rows again with how each
// one performed on the last run. Deciding whether a source was worth
// keeping meant holding both pages in your head, and "test" existed on
// both with separately-written result rendering, which is how the same
// duplicated-detail bug had to be fixed twice and the off-screen banner
// survived on one page after being fixed on the other.
//
// Merged here and joined on source id:
//   GET /sources                       the editable record
//   GET /health/sources                last-run performance for that id
//   GET /health/overview               the KPI strip
//   GET /health/runs?limit=8           recent run history
//   POST   /sources                    create
//   PATCH  /sources/{id}               update
//   POST   /sources/{id}/toggle        enable/disable
//   DELETE /sources/{id}               remove
//   POST   /health/sources/{id}/test       live fetch probe
//   POST   /health/sources/{id}/diagnose   staged failure breakdown
//
// The "Pipeline stages" card that used to sit on the health page moved
// to Run collection, where the rest of the pipeline lives. It has
// nothing to do with sources, and it was the reason an admin looking
// for a broken outlet had to scroll past five stage controls first.
//
// Health is loaded as a SUPPLEMENT, not a prerequisite: if
// /health/sources fails, the table still renders with the editable
// record and says so, because being unable to see last-run counts is
// no reason to lose the ability to disable a misbehaving source.

import { api } from "../api.js";
import { resultToast } from "../components/toast.js";
import { escapeHtml, fmtErr, fmtTimestamp, formatNumber, notWiredBanner, skeletonRows } from "../util.js";

const LANGS = ["en", "bn", "mixed"];

const STATUS_META = {
  healthy: { label: "healthy", bg: "var(--acsoft)", fg: "var(--actx)", dot: "var(--ac2)" },
  quiet: { label: "nothing new", bg: "var(--gridline)", fg: "var(--tx3)", dot: "var(--tx4)" },
  error: { label: "needs attention", bg: "var(--redsoft)", fg: "var(--red)", dot: "var(--red)" },
  disabled: { label: "disabled", bg: "var(--gridline)", fg: "var(--tx3)", dot: "var(--tx4)" },
  unknown: { label: "not run yet", bg: "var(--gridline)", fg: "var(--tx3)", dot: "var(--tx4)" },
};

const FILTERS = [
  ["all", "All"],
  ["healthy", "Healthy"],
  ["error", "Needs attention"],
  ["quiet", "Nothing new"],
  ["disabled", "Disabled"],
];

export function mountSources(root) {
  let alive = true;
  const s = {
    items: [], // GET /sources
    health: new Map(), // id -> GET /health/sources row
    overview: null,
    runs: [],
    loading: true,
    error: null, // fatal: the source list itself failed
    healthError: null, // non-fatal: counts unavailable
    testResult: null,
    testOk: true,
    addingOpen: false,
    editingId: null,
    busyIds: new Set(),
    q: "",
    filter: "all",
  };

  render();
  loadAll();

  async function loadAll() {
    s.loading = true;
    render();
    const [rec, hp, ov, rn] = await Promise.allSettled([
      api.get("/sources"),
      api.get("/health/sources"),
      api.get("/health/overview"),
      api.get("/health/runs", { query: { limit: 8 } }),
    ]);
    if (!alive) return;

    if (rec.status === "fulfilled") {
      s.items = rec.value || [];
      s.error = null;
    } else {
      s.error = rec.reason;
    }
    if (hp.status === "fulfilled") {
      s.health = new Map((hp.value || []).map((h) => [h.id, h]));
      s.healthError = null;
    } else {
      s.healthError = hp.reason;
    }
    s.overview = ov.status === "fulfilled" ? ov.value : null;
    s.runs = rn.status === "fulfilled" ? rn.value || [] : [];
    s.loading = false;
    render();
  }

  // ── derived ──────────────────────────────────────────────────────────

  function healthOf(src) {
    return s.health.get(src.id) || null;
  }

  /** One word for how a source is doing, from record and health together. */
  function statusOf(src) {
    if (!src.enabled) return "disabled";
    const h = healthOf(src);
    if (!h) return "unknown";
    return STATUS_META[h.status] ? h.status : "quiet";
  }

  function visible() {
    const q = s.q.trim().toLowerCase();
    return s.items.filter((src) => {
      if (q) {
        const hay = `${src.name} ${(src.methods || []).join(" ")} ${src.language || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return s.filter === "all" || statusOf(src) === s.filter;
    });
  }

  // ── actions ──────────────────────────────────────────────────────────

  async function testSource(src) {
    if (!src) return;
    s.busyIds.add(src.id);
    render();
    try {
      // Prefer /health/sources/{id}/test: it is the same probe as
      // /sources/{id}/test but returns the richer `detail` sentence.
      const res = await api.post(`/health/sources/${src.id}/test`);
      // `res.detail` is ALREADY the finished sentence ("102 fetched, 25
      // from today (via rss_multi)"). Rebuilding it and appending detail
      // printed the same clause twice, a bug that had to be fixed twice
      // because the two old screens each had their own copy.
      s.testResult = `${src.name}: ${res.detail || `${res.fetched} fetched, ${res.today} from today (via ${res.method})`}`;
      s.testOk = !!res.ok;
    } catch (err) {
      s.testResult = `${src.name}: ${fmtErr(err)}`;
      s.testOk = false;
    }
    s.busyIds.delete(src.id);
    render();
  }

  async function diagnoseSource(src) {
    if (!src) return;
    s.busyIds.add(src.id);
    render();
    try {
      const res = await api.post(`/health/sources/${src.id}/diagnose`);
      s.testResult = `Diagnosis for ${src.name}: ${res.detail || "no detail returned"}`;
      s.testOk = !!(res.reachable && res.feed_discovery_ok && res.article_url_found && res.date_parse_ok);
    } catch (err) {
      s.testResult = `${src.name}: ${fmtErr(err)}`;
      s.testOk = false;
    }
    s.busyIds.delete(src.id);
    render();
  }

  async function toggleEnabled(src) {
    if (!src) return;
    const prev = src.enabled;
    src.enabled = !prev; // optimistic
    render();
    try {
      const updated = await api.post(`/sources/${src.id}/toggle`);
      Object.assign(src, updated);
    } catch (err) {
      src.enabled = prev;
      s.testResult = `Couldn't change ${src.name}: ${fmtErr(err)}`;
      s.testOk = false;
    }
    render();
  }

  async function deleteSource(src) {
    if (!src) return;
    if (!window.confirm(`Delete ${src.name}? This can't be undone.`)) return;
    try {
      await api.delete(`/sources/${src.id}`);
      loadAll();
    } catch (err) {
      // Not window.alert: an alert is modal and unstyled, and on the old
      // page a blocked alert made a failed delete look like a no-op.
      s.testResult = `Couldn't delete ${src.name}: ${fmtErr(err)}`;
      s.testOk = false;
      render();
    }
  }

  // ── fragments ────────────────────────────────────────────────────────

  function kpiStrip() {
    const o = s.overview;
    if (!o) {
      return `<div class="grid-4" style="margin-bottom:20px;">${Array.from({ length: 4 })
        .map(() => `<div class="card card-sm">${skeletonRows(2)}</div>`)
        .join("")}</div>`;
    }
    return `<div class="grid-4" style="margin-bottom:20px;">
      <div class="card card-sm"><div class="kpi-value" style="color:var(--actx);">${formatNumber(o.healthy)}</div><div class="kpi-label">Healthy</div></div>
      <div class="card card-sm" style="border-color:${o.needs_attention ? "var(--red)" : "var(--line)"}">
        <div class="kpi-value" style="color:${o.needs_attention ? "var(--red)" : "var(--tx)"};">${formatNumber(o.needs_attention)}</div>
        <div class="kpi-label">Need attention</div>
      </div>
      <div class="card card-sm"><div class="kpi-value">${formatNumber(o.fetched_last_run)}</div><div class="kpi-label">Fetched last run</div></div>
      <div class="card card-sm"><div class="kpi-value">${formatNumber(o.matched_last_run)}</div><div class="kpi-label">Matched last run</div></div>
    </div>`;
  }

  function sourceRow(src) {
    const h = healthOf(src);
    const st = statusOf(src);
    const meta = STATUS_META[st];
    const on = !!src.enabled;
    const busy = s.busyIds.has(src.id);
    return `<div class="table-row sources-cols" style="opacity:${on ? 1 : 0.55};">
      <div style="min-width:0;">
        <div style="font-size:13.5px;font-weight:600;letter-spacing:-.1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(src.name)}</div>
        ${
          h && h.note
            ? `<div class="mono" style="font-size:10px;margin-top:3px;color:${st === "error" ? "var(--red)" : "var(--tx4)"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(h.note)}">${escapeHtml(h.note)}</div>`
            : ""
        }
      </div>
      <div class="row wrap" style="gap:5px;">${(src.methods || [])
        .map(
          (m) =>
            `<span class="mono" style="font-size:10.5px;color:var(--actx);background:var(--acsoft);padding:2px 8px;border-radius:5px;">${escapeHtml(m)}</span>`
        )
        .join("")}</div>
      <div class="mono" style="font-size:11px;color:var(--tx2);">${escapeHtml(src.language || "")}</div>
      <div class="mono" style="font-size:11.5px;color:var(--tx2);">${
        h ? `${formatNumber(h.fetched)} got, ${formatNumber(h.kept)} kept` : s.healthError ? "n/a" : "no run yet"
      }</div>
      <div><span class="pill" style="background:${meta.bg};color:${meta.fg};"><span class="pill-dot" style="background:${meta.dot}"></span>${escapeHtml(meta.label)}</span></div>
      <div class="row" style="gap:6px;justify-content:flex-end;">
        <button class="btn-sm" data-test="${src.id}" ${busy ? "disabled" : ""}>${busy ? "…" : "test"}</button>
        <button class="btn-sm" data-diagnose="${src.id}" ${busy ? "disabled" : ""}>diagnose</button>
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
          <label class="field-label" for="srcFormName">Name</label>
          <input id="srcFormName" class="text-input" style="padding:9px 12px;font-size:13px;" value="${escapeHtml(src ? src.name : "")}" />
        </div>
        <div>
          <label class="field-label" for="srcFormMethods">Methods, comma separated (sitemap, rss, api, gnews, id_enum, wp_rest)</label>
          <input id="srcFormMethods" class="text-input" style="padding:9px 12px;font-size:13px;" value="${escapeHtml(src ? (src.methods || []).join(", ") : "")}" />
        </div>
        <div>
          <label class="field-label" for="srcFormLang">Language</label>
          <select id="srcFormLang" class="text-input" style="padding:9px 12px;font-size:13px;">
            ${LANGS.map((l) => `<option value="${l}" ${src && src.language === l ? "selected" : ""}>${l}</option>`).join("")}
          </select>
        </div>
        <div id="srcFormError" role="alert" aria-live="assertive"
             style="display:none;font-size:12px;color:var(--red);background:color-mix(in oklab, var(--red) 12%, transparent);border:1px solid var(--red);border-radius:6px;padding:8px 11px;"></div>
        <div class="row" style="gap:9px;">
          <button id="srcFormSave" class="btn btn-primary" style="padding:9px 16px;">${isEdit ? "Save" : "Add source"}</button>
          <button id="srcFormCancel" class="btn btn-outline" style="padding:9px 16px;">Cancel</button>
        </div>
      </div>
    </div>`;
  }

  /**
   * Status text for a run row.
   *
   * `heartbeat_stale` is computed by the API precisely because a run
   * that is hard-killed stays `status='running'` in the table forever,
   * until the next batch's reaper sweeps it. The old screen ignored the
   * flag and printed `status` alone, so a dead run and a live one were
   * indistinguishable and an operator would sit waiting on a run that
   * had already died.
   */
  function runStatusText(r) {
    if (r.status === "running" && r.heartbeat_stale) return "stalled";
    return r.status;
  }

  function runStatusColor(r) {
    if (r.status === "done") return "var(--green)";
    if (r.status === "running" && r.heartbeat_stale) return "var(--red)";
    return "var(--amber)";
  }

  function runsCard() {
    return `<div class="table-wrap">
      <div style="padding:20px 22px 14px;">
        <h3 style="margin:0 0 4px;">Recent runs</h3>
        <p class="hint" style="margin:0;">Whole-desk totals, so you can tell a bad run from a bad source.</p>
      </div>
      <div class="table-head-row runs-cols" style="border-top:1px solid var(--line);">
        <div class="table-th">Started</div><div class="table-th">Trigger</div>
        <!-- "New", not "Fetched": the value is runs.total_new, the count
             of articles actually INSERTED. Labelling it Fetched put 707
             under a header that the KPI tile above answers with 7,023,
             which reads as a contradiction rather than as two different
             measurements. -->
        <div class="table-th">New</div><div class="table-th">Matched</div><div class="table-th">Status</div>
      </div>
      ${
        s.runs.length
          ? s.runs
              .map(
                (r) => `<div class="table-row runs-cols">
          <div class="mono" style="font-size:12px;color:var(--tx2);">${escapeHtml(fmtTimestamp(r.when))}</div>
          <div><span class="mono" style="font-size:10.5px;color:var(--tx2);background:var(--raise);border:1px solid var(--line);padding:2px 8px;border-radius:5px;">${escapeHtml(r.trigger)}</span></div>
          <div class="mono" style="font-size:12px;color:var(--tx2);">${formatNumber(r.new_count)}</div>
          <div class="mono" style="font-size:12px;color:var(--tx2);">${formatNumber(r.matched_count)}</div>
          <div style="font-size:11px;color:${runStatusColor(r)};">${escapeHtml(runStatusText(r))}</div>
        </div>`
              )
              .join("")
          : `<div style="padding:16px 22px;font-size:12px;color:var(--tx3);">No runs recorded yet.</div>`
      }
    </div>`;
  }

  // ── render ───────────────────────────────────────────────────────────

  function render() {
    const rows = visible();
    const editing = s.editingId ? s.items.find((x) => x.id === s.editingId) : null;

    root.innerHTML = `<div class="page page-wide">
      <div class="row-between wrap" style="margin-bottom:20px;">
        <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0;max-width:60ch;">
          Every outlet feeding the desk, with how each one did on the last collection run.
          Test fetches from the site right now. Diagnose breaks a failure down by stage, so you can
          see whether the problem is reachability, feed discovery, article URLs or date parsing.
        </p>
        <button id="addSourceBtn" class="btn btn-primary" style="padding:11px 18px;white-space:nowrap;">+ Add source</button>
      </div>

      ${kpiStrip()}

      ${s.addingOpen ? sourceForm(null) : ""}

      ${
        s.healthError && !s.error
          ? `<div class="banner banner-warn"><span>&#9888;</span><div style="flex:1;">Last-run counts are unavailable (${escapeHtml(fmtErr(s.healthError))}). The source list below is still live.</div></div>`
          : ""
      }

      ${
        s.error
          ? s.error.kind === "forbidden"
            ? `<div class="banner banner-error"><span>&#128274;</span><div>Sources are admin only. Your account doesn't have the admin role.</div></div>`
            : s.error.kind === "not_found"
            ? notWiredBanner("GET /sources")
            : `<div class="banner banner-error"><span>&#10005;</span><div style="flex:1;">${escapeHtml(fmtErr(s.error))}</div><button id="sourcesRetry" class="btn-ghost" style="color:var(--red);text-decoration:underline;">Retry</button></div>`
          : `<div class="table-wrap" style="margin-bottom:16px;">
            <div class="row wrap" style="gap:12px;align-items:center;padding:16px 22px 13px;">
              <div class="search-box" style="flex:1;min-width:190px;">
                <span style="color:var(--tx4);font-size:12px;">&#8981;</span>
                <input id="srcFilterInput" placeholder="Filter by name, method or language" aria-label="Filter sources by name, method or language" value="${escapeHtml(s.q)}" />
              </div>
              <div class="row" style="gap:5px;">
                ${FILTERS.map(
                  ([k, label]) =>
                    `<button class="btn-sm ${s.filter === k ? "btn-sm-on" : ""}" data-filter="${k}">${label}</button>`
                ).join("")}
              </div>
              <span class="mono" style="font-size:10.5px;color:var(--tx4);white-space:nowrap;">${formatNumber(rows.length)} of ${formatNumber(s.items.length)}</span>
            </div>
            <div class="table-head-row sources-cols" style="border-top:1px solid var(--line);">
              <div class="table-th">Source</div><div class="table-th">Methods</div><div class="table-th">Lang</div><div class="table-th">Last run</div><div class="table-th">Status</div><div class="table-th" style="text-align:right;">Actions</div>
            </div>
            ${
              s.loading
                ? Array.from({ length: 6 })
                    .map(() => `<div class="table-row sources-cols">${skeletonRows(1).repeat(6)}</div>`)
                    .join("")
                : rows.length
                ? rows.map((src) => (s.editingId === src.id ? sourceForm(src) : sourceRow(src))).join("")
                : s.items.length
                ? `<div class="state-block"><div class="state-title">No sources match</div><p class="state-body">Clear the filter or pick a different status.</p></div>`
                : `<div class="state-block"><div class="state-title">No sources configured</div><p class="state-body">Add the first outlet to start collecting.</p></div>`
            }
          </div>
          ${runsCard()}`
      }

      ${
        // FIXED position, not inline. This banner used to render above a
        // 51-row table, so testing a source near the bottom put its
        // result far out of view and the button looked dead.
        resultToast({ message: s.testResult, ok: s.testOk, dismissId: "clearTestBtn" })
      }
    </div>`;

    // `editing` is rendered inline by sourceRow's sibling branch above,
    // but a source that the current filter hides would otherwise take
    // its form with it. Fall back to showing the form at the top.
    if (editing && !rows.some((r) => r.id === editing.id)) {
      const page = root.querySelector(".page");
      if (page) page.insertAdjacentHTML("afterbegin", sourceForm(editing));
    }

    bind();
  }

  // ── binding ──────────────────────────────────────────────────────────

  function bind() {
    const byAttr = (b, name) => s.items.find((x) => x.id === Number(b.getAttribute(name)));

    root.querySelectorAll("[data-test]").forEach((b) => b.addEventListener("click", () => testSource(byAttr(b, "data-test"))));
    root.querySelectorAll("[data-diagnose]").forEach((b) => b.addEventListener("click", () => diagnoseSource(byAttr(b, "data-diagnose"))));
    root.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => toggleEnabled(byAttr(b, "data-toggle"))));
    root.querySelectorAll("[data-delete]").forEach((b) => b.addEventListener("click", () => deleteSource(byAttr(b, "data-delete"))));
    root.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => {
        s.editingId = Number(b.getAttribute("data-edit"));
        s.addingOpen = false;
        render();
      })
    );
    root.querySelectorAll("[data-filter]").forEach((b) =>
      b.addEventListener("click", () => {
        s.filter = b.getAttribute("data-filter");
        render();
      })
    );

    const addBtn = root.querySelector("#addSourceBtn");
    if (addBtn) addBtn.addEventListener("click", () => { s.addingOpen = true; s.editingId = null; render(); });

    const retry = root.querySelector("#sourcesRetry");
    if (retry) retry.addEventListener("click", () => loadAll());

    const clearTestBtn = root.querySelector("#clearTestBtn");
    if (clearTestBtn) clearTestBtn.addEventListener("click", () => { s.testResult = null; render(); });

    // Typing rebuilds the table, which destroys the focused input unless
    // focus and caret are restored. Exactly the failure the review queue
    // had: the filter looked broken because it swallowed every character
    // after the first.
    const filterInput = root.querySelector("#srcFilterInput");
    if (filterInput) {
      filterInput.addEventListener("input", (e) => {
        const caret = e.target.selectionStart;
        s.q = e.target.value;
        render();
        const again = root.querySelector("#srcFilterInput");
        if (!again) return;
        again.focus();
        try {
          again.setSelectionRange(caret, caret);
        } catch {
          /* some input types reject setSelectionRange */
        }
      });
    }

    const saveBtn = root.querySelector("#srcFormSave");
    if (saveBtn) {
      saveBtn.addEventListener("click", async () => {
        const name = root.querySelector("#srcFormName").value.trim();
        const methods = root
          .querySelector("#srcFormMethods")
          .value.split(",")
          .map((x) => x.trim())
          .filter(Boolean);
        const language = root.querySelector("#srcFormLang").value;

        // Inline, not window.alert(). An alert is modal, unstyled, and
        // silently suppressed in some contexts, so an invalid submit
        // looked like the button did nothing. `methods` was also never
        // validated, so a source could be created with an empty method
        // list: accepted by the API, visible in the table, and incapable
        // of ever fetching anything.
        const errEl = root.querySelector("#srcFormError");
        const showError = (msg) => {
          if (!errEl) return;
          errEl.textContent = msg;
          errEl.style.display = "";
        };
        if (errEl) errEl.style.display = "none";
        if (!name) return showError("Name is required.");
        if (!methods.length) {
          return showError("At least one method is required, for example sitemap, rss, api, gnews or id_enum.");
        }

        saveBtn.disabled = true;
        try {
          if (s.editingId) await api.patch(`/sources/${s.editingId}`, { name, methods, language });
          else await api.post("/sources", { name, methods, language, enabled: true });
          s.addingOpen = false;
          s.editingId = null;
          loadAll();
        } catch (err) {
          // `ApiError.message` already carries the API's `detail`, which
          // names the offending field, so this surfaces "methods: Field
          // required" rather than a generic "couldn't save".
          showError("Couldn't save: " + fmtErr(err));
        } finally {
          saveBtn.disabled = false;
        }
      });
    }

    const cancelBtn = root.querySelector("#srcFormCancel");
    if (cancelBtn) cancelBtn.addEventListener("click", () => { s.addingOpen = false; s.editingId = null; render(); });
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

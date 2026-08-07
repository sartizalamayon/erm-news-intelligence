// Source health admin (frontend-contract.md §3.10).
//
// Real API — app/api/routers/health.py (ADMIN-API, landed):
//   GET  /health/overview -> { healthy, needs_attention, fetched_last_run,
//                              matched_last_run, last_run_at,
//                              stages: [{key,name,desc,status,ran_at,detail}] }
//   GET  /health/sources  -> [{ id,name,method,fetched,kept,status,note }]
//   GET  /health/runs?limit= -> [{ id,when,trigger,new_count,matched_count,status }]
//   POST /health/sources/{id}/test      -> SourceTestResult
//   POST /health/sources/{id}/diagnose  -> SourceDiagnoseResult (real per-check
//        diagnostics, not the mock's fabricated fixed string — §3.10/§9)
//   POST /health/pipeline/stages/{key}/run -> StageRunResult
//   POST /health/pipeline/run-all          -> StageRunResult[]
//
// The KPI tiles (Healthy / Need attention / Fetched / Matched) are
// computed server-side from real healthRows/runRows data — §3.10 flags
// these as the one screen where the prototype hardcodes them; this
// build renders whatever the server sends, no client-side fabrication.

import { api } from "../api.js";
import { setState } from "../state.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows } from "../util.js";

const STATUS_META = {
  healthy: { label: "healthy", bg: "var(--acsoft)", fg: "var(--actx)", dot: "var(--ac2)" },
  quiet: { label: "nothing new", bg: "var(--gridline)", fg: "var(--tx3)", dot: "var(--tx4)" },
  error: { label: "needs attention", bg: "var(--redsoft)", fg: "var(--red)", dot: "var(--red)" },
  disabled: { label: "disabled", bg: "var(--gridline)", fg: "var(--tx3)", dot: "var(--tx4)" },
};

export function mountHealth(root) {
  let alive = true;
  const s = {
    overview: null,
    overviewError: null,
    sources: [],
    sourcesError: null,
    runs: [],
    testResult: null,
    testOk: true,
    busyStage: null,
    busySource: null,
    runningAll: false,
  };

  render();
  loadAll();

  function loadAll() {
    loadOverview();
    loadSources();
    loadRuns();
  }

  async function loadOverview() {
    try {
      const data = await api.get("/health/overview");
      if (!alive) return;
      s.overview = data;
      s.overviewError = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.overviewError = err;
      render();
    }
  }

  async function loadSources() {
    try {
      const data = await api.get("/health/sources");
      if (!alive) return;
      s.sources = data || [];
      s.sourcesError = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.sourcesError = err;
      render();
    }
  }

  async function loadRuns() {
    try {
      const data = await api.get("/health/runs", { query: { limit: 5 } });
      if (!alive) return;
      s.runs = data || [];
      render();
    } catch (err) {
      /* runs table just stays empty */
    }
  }

  async function runStage(key) {
    s.busyStage = key;
    setState({ progress: key + " · running" });
    render();
    try {
      await api.post(`/health/pipeline/stages/${key}/run`);
    } catch (err) {
      window.alert("Stage run failed: " + fmtErr(err));
    }
    s.busyStage = null;
    setState({ progress: "" });
    loadOverview();
    render();
  }

  async function runAll() {
    s.runningAll = true;
    setState({ progress: "All stages · running" });
    render();
    try {
      await api.post("/health/pipeline/run-all");
    } catch (err) {
      window.alert("Run-all failed: " + fmtErr(err));
    }
    s.runningAll = false;
    setState({ progress: "" });
    loadAll();
    render();
  }

  async function testSource(src) {
    s.busySource = src.id;
    render();
    try {
      const res = await api.post(`/health/sources/${src.id}/test`);
      s.testResult = `${src.name} — ${res.fetched} fetched, ${res.today} from today (via ${res.method})${res.detail ? " · " + res.detail : ""}`;
      s.testOk = !!res.ok;
    } catch (err) {
      s.testResult = `${src.name} — ${fmtErr(err)}`;
      s.testOk = false;
    }
    s.busySource = null;
    render();
  }

  async function diagnoseSource(src) {
    s.busySource = src.id;
    render();
    try {
      const res = await api.post(`/health/sources/${src.id}/diagnose`);
      s.testResult = `Diagnosis: ${src.name} — reachable ${mark(res.reachable)} · feed discovery ${mark(res.feed_discovery_ok)} · article URLs ${mark(
        res.article_url_found
      )} · date parse ${mark(res.date_parse_ok)}${res.detail ? " · " + res.detail : ""}`;
      s.testOk = res.reachable && res.feed_discovery_ok && res.article_url_found && res.date_parse_ok;
    } catch (err) {
      s.testResult = `${src.name} — ${fmtErr(err)}`;
      s.testOk = false;
    }
    s.busySource = null;
    render();
  }

  function mark(b) {
    return b ? "✓" : "✕";
  }

  function render() {
    const o = s.overview;
    root.innerHTML = `
      <div class="page page-wide">
        <div class="grid-4" style="margin-bottom:20px;">
          ${
            s.overviewError
              ? `<div class="card card-sm" style="grid-column:1/-1;">${
                  s.overviewError.kind === "forbidden"
                    ? `<div style="color:var(--red);">Source health is admin-only. Your account doesn't have the admin role.</div>`
                    : notWiredBanner("GET /health/overview")
                }</div>`
              : !o
              ? Array.from({ length: 4 })
                  .map(() => `<div class="card card-sm">${skeletonRows(2)}</div>`)
                  .join("")
              : `
              <div class="card card-sm"><div class="kpi-value" style="color:var(--actx);">${o.healthy}</div><div class="kpi-label">Healthy</div></div>
              <div class="card card-sm" style="border-color:${o.needs_attention ? "var(--red)" : "var(--line)"}"><div class="kpi-value" style="color:${
                  o.needs_attention ? "var(--red)" : "var(--tx)"
                };">${o.needs_attention}</div><div class="kpi-label">Need attention</div></div>
              <div class="card card-sm"><div class="kpi-value">${o.fetched_last_run.toLocaleString()}</div><div class="kpi-label">Fetched last run</div></div>
              <div class="card card-sm"><div class="kpi-value">${o.matched_last_run.toLocaleString()}</div><div class="kpi-label">Matched last run</div></div>
            `
          }
        </div>

        <div class="card" style="margin-bottom:16px;">
          <div class="row-between wrap" style="margin-bottom:18px;">
            <div>
              <h3 style="margin-bottom:4px;">Pipeline stages</h3>
              <p class="hint" style="margin:0;">Stage by stage. Run one again after changing keywords or guidance.${
                o && o.last_run_at ? ` Last run ${escapeHtml(o.last_run_at)}.` : ""
              }</p>
            </div>
            <button id="runAllBtn" class="btn btn-soft" style="white-space:nowrap;" ${s.runningAll ? "disabled" : ""}>&#9654; ${s.runningAll ? "Running…" : "Run all stages"}</button>
          </div>
          <div class="grid-3">
            ${
              o
                ? o.stages
                    .map((st) => {
                      const done = st.status === "done";
                      const err = st.status === "error";
                      return `<div style="display:flex;gap:13px;padding:15px 16px;border:1px solid ${err ? "var(--red)" : done ? "var(--line)" : "var(--line2)"};border-radius:12px;background:var(--panel2);">
                <div style="flex:0 0 30px;height:30px;border-radius:9px;display:grid;place-items:center;font-size:12.5px;font-weight:700;color:var(--onac);background:${
                  err ? "var(--red)" : "var(--ac)"
                };">${escapeHtml(st.key[0].toUpperCase())}</div>
                <div style="flex:1;min-width:0;">
                  <div class="row" style="gap:8px;">
                    <div style="font-size:13.5px;font-weight:600;letter-spacing:-.2px;">${escapeHtml(st.name)}</div>
                    <span class="mono" style="font-size:9px;letter-spacing:.8px;text-transform:uppercase;padding:2px 7px;border-radius:999px;background:${
                      err ? "var(--redsoft)" : done ? "var(--acsoft)" : "var(--gridline)"
                    };color:${err ? "var(--red)" : done ? "var(--actx)" : "var(--tx3)"};">${escapeHtml(st.status.replace("_", " "))}</span>
                  </div>
                  <div style="font-size:11.5px;color:var(--tx3);line-height:1.45;margin-top:4px;">${escapeHtml(st.desc)}</div>
                  ${st.detail ? `<div class="mono" style="font-size:10.5px;color:${err ? "var(--red)" : "var(--tx3)"};margin-top:4px;">${escapeHtml(st.detail)}</div>` : ""}
                  <button class="btn-sm" data-run-stage="${st.key}" style="margin-top:10px;" ${s.busyStage === st.key ? "disabled" : ""}>${
                        s.busyStage === st.key ? "Running…" : done ? "Run again" : "Run now"
                      }</button>
                </div>
              </div>`;
                    })
                    .join("")
                : ""
            }
          </div>
        </div>

        ${
          s.testResult
            ? `<div class="banner ${s.testOk ? "banner-ok" : "banner-error"}"><span>${s.testOk ? "&#10003;" : "&#10005;"}</span><div style="flex:1;">${escapeHtml(
                s.testResult
              )}</div><button id="clearTestBtn" class="btn-ghost" style="font-size:14px;">&#10005;</button></div>`
            : ""
        }

        <div class="table-wrap" style="margin-bottom:16px;">
          <div style="padding:20px 22px 14px;">
            <h3 style="margin-bottom:4px;">Source health</h3>
            <p class="hint" style="margin:0;">"No articles" can mean a quiet day or a broken parser — the note tells you which.</p>
          </div>
          <div class="table-head-row health-cols" style="border-top:1px solid var(--line);">
            <div class="table-th">Source</div><div class="table-th">Method</div><div class="table-th">Got</div><div class="table-th">Kept</div><div class="table-th">Status</div><div class="table-th" style="text-align:right;">Actions</div>
          </div>
          ${
            s.sourcesError
              ? `<div style="padding:16px 22px;">${
                  s.sourcesError.kind === "forbidden"
                    ? `<div style="color:var(--red);font-size:13px;">Admin only.</div>`
                    : notWiredBanner("GET /health/sources")
                }</div>`
              : !s.sources.length
              ? Array.from({ length: 5 })
                  .map(() => `<div class="table-row health-cols">${skeletonRows(1).repeat(5)}</div>`)
                  .join("")
              : s.sources
                  .map((h) => {
                    const meta = STATUS_META[h.status] || STATUS_META.quiet;
                    return `<div class="table-row health-cols">
                <div style="font-size:13px;font-weight:600;">${escapeHtml(h.name)}</div>
                <div><span class="mono" style="font-size:10.5px;color:var(--actx);background:var(--acsoft);padding:2px 8px;border-radius:5px;">${escapeHtml(h.method)}</span></div>
                <div class="mono" style="font-size:12px;color:var(--tx2);">${h.fetched}</div>
                <div class="mono" style="font-size:12px;color:var(--tx2);">${h.kept}</div>
                <div class="row" style="gap:9px;min-width:0;">
                  <span class="pill" style="background:${meta.bg};color:${meta.fg};flex:0 0 auto;"><span class="pill-dot" style="background:${meta.dot}"></span>${meta.label}</span>
                  ${h.note ? `<span class="mono" style="font-size:10px;color:${h.status === "error" ? "var(--red)" : "var(--tx3)"};overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(h.note)}</span>` : ""}
                </div>
                <div class="row" style="gap:6px;justify-content:flex-end;">
                  <button class="btn-sm" data-test-source="${h.id}" ${s.busySource === h.id ? "disabled" : ""}>test</button>
                  <button class="btn-sm" data-diagnose-source="${h.id}" ${s.busySource === h.id ? "disabled" : ""}>diagnose</button>
                </div>
              </div>`;
                  })
                  .join("")
          }
        </div>

        <div class="table-wrap">
          <div style="padding:20px 22px 14px;"><h3 style="margin:0;">Recent runs</h3></div>
          <div class="table-head-row runs-cols" style="border-top:1px solid var(--line);">
            <div class="table-th">Started</div><div class="table-th">Trigger</div><div class="table-th">Fetched</div><div class="table-th">Matched</div><div class="table-th">Status</div>
          </div>
          ${
            s.runs.length
              ? s.runs
                  .map(
                    (r) => `<div class="table-row runs-cols">
              <div class="mono" style="font-size:12px;color:var(--tx2);">${escapeHtml(r.when)}</div>
              <div><span class="mono" style="font-size:10.5px;color:var(--tx2);background:var(--raise);border:1px solid var(--line);padding:2px 8px;border-radius:5px;">${escapeHtml(r.trigger)}</span></div>
              <div class="mono" style="font-size:12px;color:var(--tx2);">${r.new_count.toLocaleString()}</div>
              <div class="mono" style="font-size:12px;color:var(--tx2);">${r.matched_count.toLocaleString()}</div>
              <div style="font-size:11px;color:${r.status === "done" ? "var(--green)" : "var(--amber)"};">${escapeHtml(r.status)}</div>
            </div>`
                  )
                  .join("")
              : `<div style="padding:16px 22px;font-size:12px;color:var(--tx3);">No runs recorded yet.</div>`
          }
        </div>
      </div>
    `;
    bind();
  }

  function bind() {
    const runAllBtn = root.querySelector("#runAllBtn");
    if (runAllBtn) runAllBtn.addEventListener("click", runAll);
    root.querySelectorAll("[data-run-stage]").forEach((b) => b.addEventListener("click", () => runStage(b.getAttribute("data-run-stage"))));
    const clearTestBtn = root.querySelector("#clearTestBtn");
    if (clearTestBtn) clearTestBtn.addEventListener("click", () => { s.testResult = null; render(); });
    root.querySelectorAll("[data-test-source]").forEach((b) => b.addEventListener("click", () => testSource(s.sources.find((x) => x.id == b.getAttribute("data-test-source")))));
    root.querySelectorAll("[data-diagnose-source]").forEach((b) => b.addEventListener("click", () => diagnoseSource(s.sources.find((x) => x.id == b.getAttribute("data-diagnose-source")))));
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

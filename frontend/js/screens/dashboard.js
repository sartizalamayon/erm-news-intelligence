// Dashboard — Short term + Long term tabs (frontend-contract.md §3.1/§3.2,
// docs/dashboard-longterm-spec.md). Both endpoints are real and live —
// app/api/routers/dashboard.py:
//
//   GET /dashboard/today ->
//     { pendingCount, approvedCount, todayLabel,
//       kpis: [{ value, label, delta, note, vals: [14 real daily counts, may be empty] }, ...4],
//       funnel: [{ phase, label, value, pct, note }, ...4],
//       days14: [{ date: "YYYY-MM-DD", count }, ...14],
//       relMix: [{ key: "red"|"yellow"|"green"|"magenta", count }, ...4],
//       bySection: [{ label, count }, ...],  // one row per REAL configured section (get_risk_sections), not a fixed 5
//       topSources: [{ label, count }, ...] }
//
//   GET /dashboard/trends?range=12mo ->
//     { trendCards: [{ label, unit, vals: [12 numbers], bad, decimals }, ...4],  // macro indicators
//       months12: [{ label, segments: { <section>: count, ... } }, ...12],       // primary chart, per dashboard-longterm-spec.md
//       heatRows: [{ label, values: [12 floats 0..1], trend }, ...],
//       standing: [{ name, note, count, months, dir: "up"|"down"|"flat" }, ...],
//       relMixTrend: [{ label, highShare: 0..1, total }, ...12],  // global, not per-section — see RelMixTrendPointOut
//       topSources: [{ label, count }, ...] }                    // 12mo aggregate, not per-section — see DashboardTrendsOut.topSources
//
// Bar-width / heatmap-cell scaling is computed here from the REAL
// maximum in the returned set (util.pctOfMax) — not the prototype's
// hardcoded-denominator bugs (§8 item 4: `/214`, `/12`).

import { api } from "../api.js";
import { setState, state } from "../state.js";
import {
  escapeHtml,
  formatNumber,
  pctOfMax,
  pctOfTotal,
  poly,
  tier,
  RISK_SECTIONS,
  ensureRiskSections,
  sectionColor,
  fmtErr,
  skeletonRows,
  notWiredBanner,
} from "../util.js";

// Fixed 5-stage pipeline (app/services/admin.py's PIPELINE_STAGES) used
// only as a fallback if GET /health/overview can't be reached before a
// run starts — the real run always drives off whatever stage list that
// endpoint actually returns.
const PIPELINE_STAGE_FALLBACK = [
  { key: "translate", name: "Translate", desc: "Bengali headlines → English" },
  { key: "bodies", name: "Get article text", desc: "Fetch full article bodies for today's matched stories" },
  { key: "dedup", name: "Group duplicates", desc: "Cluster near-duplicate coverage of the same event" },
  { key: "judge", name: "Score relevance", desc: "LLM relevance scoring, 1–10" },
  { key: "topic", name: "Group topics", desc: "Cluster scored articles into broad topics" },
];

const REL_TIER_META = {
  red: { label: "High", band: "8–10", color: "var(--red)" },
  yellow: { label: "Medium", band: "6–7", color: "var(--amber)" },
  green: { label: "Low", band: "5", color: "var(--green)" },
  magenta: { label: "Least", band: "<5", color: "var(--violet)" },
};

const DAY_LETTERS = ["S", "M", "T", "W", "T", "F", "S"]; // Date#getDay() index 0=Sun

// SHORT-TERM: direction-colored deltas, applied uniformly. Previously
// hardcoded to var(--tx3) (neutral) for every KPI regardless of sign,
// while the long-term trend cards already color by direction
// (dashboard-longterm-spec.md's short-term section flags this exact
// inconsistency). KpiOut.delta is always a pre-signed string
// ("+3.2%"/"-4.1%", app/api/routers/dashboard.py's `_pct_delta`), so the
// sign is read directly off it rather than re-deriving direction.
function deltaColor(delta) {
  const s = String(delta || "").trim();
  if (s.startsWith("-")) return "var(--red)";
  if (s.startsWith("+")) return "var(--green)";
  return "var(--tx3)";
}

// SHORT-TERM: real 14-day sparkline per KPI tile, from KpiOut.vals
// (app/api/routers/dashboard.py — real grouped daily counts, not a
// decorative series). Renders nothing when the backend sent no series
// (e.g. "Awaiting review", a point-in-time queue depth with no daily
// history to show) rather than fabricating one.
function kpiSparklineHtml(vals) {
  if (!vals || vals.length < 2) return "";
  const pts = poly(vals, 100, 26, false);
  return `<svg viewBox="0 0 100 26" preserveAspectRatio="none" style="width:100%;height:22px;margin:6px 0 2px;display:block;overflow:visible;">
    <polyline points="${pts}" fill="none" stroke="var(--tx4)" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"></polyline>
  </svg>`;
}

// LONG-TERM macro cards: TrendCardOut.status/asOf/note
// (app/api/schemas/core.py, wired by app/services/macro.py) make where
// the card's CURRENT value came from this request visible instead of
// hidden — "live" (a real external fetch, cached a few hours),
// "unconfigured" (a live source that needs setup, e.g. Brent's
// oilpriceapi_key), or "rollup" (no live source layered on, e.g. a
// fetch failure — never a fabricated number).
const MACRO_STATUS_META = {
  live: { label: "Live", color: "var(--green)" },
  unconfigured: { label: "Not configured", color: "var(--amber)" },
};
function macroStatusHtml(t) {
  const meta = MACRO_STATUS_META[t.status];
  const asOfText = t.asOf
    ? new Date(t.asOf).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
    : "";
  const caption = [asOfText, t.note].filter(Boolean).join(" · ");
  if (!meta && !caption) return "";
  return `<div class="row" style="gap:6px;margin-top:9px;align-items:flex-start;">
    ${meta ? `<span class="pill" style="flex:0 0 auto;background:color-mix(in oklab, ${meta.color} 20%, transparent);color:${meta.color};font-size:9px;padding:2px 7px;">${meta.label}</span>` : ""}
    ${caption ? `<span style="font-size:10px;color:var(--tx4);flex:1;min-width:0;line-height:1.3;">${escapeHtml(caption)}</span>` : ""}
  </div>`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function mountDashboard(root, { navigate }) {
  let alive = true;
  const mode = state.dashMode || "short";
  let lastData = null; // cached last-loaded payload, so pipeline ticks can re-render without re-fetching
  // { active, startedAt, finishedAt,
  //   fetch: null | { status, detail, sources: [{name,status,fetched,inserted,duplicate,error}], total, completed },
  //   match: null | { status, detail },
  //   stages: [{key,name,desc,status,detail,startedAt,finishedAt}] }
  let pipeline = null;
  let tickTimer = null;
  // Phase selection for "Run collection" (BUGFIX: this used to be a
  // single undifferentiated button that only ever ran the 5 enrichment
  // stages — see runCollection()'s own docstring below). All three
  // phases start checked so one click still does the full "nothing new
  // -> today's news, matched, scored, ready to review" pass by default;
  // an admin can uncheck any phase to run a subset.
  const phases = { fetch: true, match: true, enrich: true };

  ensureRiskSections();
  render(loadingShell(mode));
  load();

  async function load() {
    try {
      const path = mode === "short" ? "/dashboard/today" : "/dashboard/trends";
      const data = await api.get(path, mode === "short" ? undefined : { query: { range: "12mo" } });
      if (!alive) return;
      lastData = data || {};
      if (mode === "short") renderShort(lastData);
      else renderLong(lastData);
    } catch (err) {
      if (!alive) return;
      renderError(err);
    }
  }

  function render(html) {
    root.innerHTML = `<div class="page">${html}</div>`;
    bindHeader();
  }

  function bindHeader() {
    root.querySelectorAll("[data-mode]").forEach((btn) => {
      btn.addEventListener("click", () => setState({ dashMode: btn.getAttribute("data-mode") }));
    });
    const runCollectionBtn = root.querySelector("#runCollectionBtn");
    if (runCollectionBtn) runCollectionBtn.addEventListener("click", () => runCollection());
    root.querySelectorAll("[data-phase]").forEach((cb) => {
      cb.addEventListener("change", () => {
        phases[cb.getAttribute("data-phase")] = cb.checked;
      });
    });
    const buildExportBtn = root.querySelector("#buildExportBtn");
    if (buildExportBtn) buildExportBtn.addEventListener("click", () => navigate("export"));
  }

  // Re-renders whatever's currently on screen from the cached payload —
  // used to tick the pipeline-progress panel (elapsed time, per-stage
  // status) without a network round trip.
  function rerenderCurrent() {
    if (!alive || lastData === null) return;
    if (mode === "short") renderShort(lastData);
    else renderLong(lastData);
  }

  function startTicking() {
    stopTicking();
    tickTimer = setInterval(() => {
      if (!alive) return stopTicking();
      rerenderCurrent();
    }, 500);
  }
  function stopTicking() {
    if (tickTimer) clearInterval(tickTimer);
    tickTimer = null;
  }

  // BUGFIX: "Run all stages" used to be the single most misleading
  // button in the app — it only ever ran the 5 LLM enrichment stages
  // below, never a real fetch, so it reported "done, 0s elapsed" every
  // time regardless of whether anything new had actually been
  // collected. Nothing fetched new articles outside the daily GitHub
  // Actions cron because no HTTP endpoint anywhere called
  // app.services.collector.run_collection. Renamed "Run collection" and
  // rebuilt as up to three real, independently-selectable phases (see
  // the `phases` checkboxes in header()):
  //   1. Fetch  — POST /health/pipeline/fetch/run, a real P1 collection
  //      run for today, backgrounded server-side (a full ~45-source run
  //      takes low minutes — see app.services.admin's "Fetch (P1)
  //      trigger" module docstring) and polled via GET
  //      /health/pipeline/fetch/status for genuine per-source progress
  //      while it's in flight, never an instant fake "done".
  //   2. Match  — POST /health/pipeline/match/run, P2 keyword matching
  //      against today's articles (confirmed a genuinely separate step
  //      from fetch by reading app.services.collector.run_collection's
  //      own docstring — P1 never computes sections/keywords itself).
  //   3. Enrich — the pre-existing 5 LLM stages, unchanged: driven one
  //      at a time via the real per-stage endpoint
  //      (frontend/js/screens/health.js's `runStage` uses the identical
  //      pattern), each stage's pending → running → done/error
  //      transition a real event off a real network response.
  // Phases run in order (fetch, then match, then enrich) when more than
  // one is selected — matching depends on fetch having landed rows to
  // match, and enrichment depends on matching having decided which rows
  // are even in play — but one phase failing does not block the next
  // (same per-unit isolation philosophy app.services.collector already
  // applies per-source and run_pipeline_all already applies per-stage).
  async function runCollection() {
    if (pipeline && pipeline.active) return;
    const selected = { ...phases };
    if (!selected.fetch && !selected.match && !selected.enrich) return;

    let stageDefs = PIPELINE_STAGE_FALLBACK;
    if (selected.enrich) {
      try {
        const overview = await api.get("/health/overview");
        if (overview && Array.isArray(overview.stages) && overview.stages.length) {
          stageDefs = overview.stages.map((s) => ({ key: s.key, name: s.name, desc: s.desc }));
        }
      } catch (err) {
        // Falls back to the fixed 5-stage list above — still real stage
        // keys/endpoints, just without live names/descriptions from the API.
      }
    }
    if (!alive) return;

    pipeline = {
      active: true,
      startedAt: Date.now(),
      finishedAt: null,
      fetch: selected.fetch ? { status: "pending", detail: "", sources: [], total: 0, completed: 0 } : null,
      match: selected.match ? { status: "pending", detail: "" } : null,
      stages: selected.enrich
        ? stageDefs.map((s) => ({ ...s, status: "pending", detail: "", startedAt: null, finishedAt: null }))
        : [],
    };
    setState({ progress: "Run collection · starting…" });
    startTicking();
    rerenderCurrent();

    if (pipeline.fetch) await runFetchPhase();
    if (alive && pipeline.match) await runMatchPhase();
    if (alive) await runEnrichPhase();

    pipeline.active = false;
    pipeline.finishedAt = Date.now();
    stopTicking();
    setState({ progress: "" });
    rerenderCurrent();
  }

  // Phase 1: fetch. Starts the backgrounded server-side run, then polls
  // GET /health/pipeline/fetch/status every ~1.5s for the genuinely-live
  // per-source progress app.services.admin.start_fetch_run/fetch_status
  // track (which sources have finished, how many are new) — not a
  // client-side timer standing in for progress.
  async function runFetchPhase() {
    pipeline.fetch.status = "running";
    rerenderCurrent();
    try {
      await api.post("/health/pipeline/fetch/run");
      let terminal = false;
      while (alive && !terminal) {
        let poll;
        try {
          poll = await api.get("/health/pipeline/fetch/status");
        } catch (err) {
          pipeline.fetch.status = "error";
          pipeline.fetch.detail = fmtErr(err);
          break;
        }
        if (!alive) return;
        pipeline.fetch.total = poll.total_sources || 0;
        pipeline.fetch.completed = poll.completed || 0;
        pipeline.fetch.sources = poll.sources || [];
        setState({
          progress: `Run collection · fetching (${pipeline.fetch.completed}/${pipeline.fetch.total || "?"} sources)`,
        });
        rerenderCurrent();
        if (poll.status === "done" || poll.status === "error") {
          terminal = true;
          pipeline.fetch.status = poll.status;
          pipeline.fetch.detail =
            poll.status === "error"
              ? poll.error || "fetch failed"
              : `${poll.total_inserted || 0} new article(s) across ${poll.total_sources || 0} source(s)`;
        } else {
          await sleep(1500);
        }
      }
    } catch (err) {
      pipeline.fetch.status = "error";
      pipeline.fetch.detail = fmtErr(err);
    }
    if (!alive) return;
    rerenderCurrent();
  }

  // Phase 2: match. Synchronous — a local regex pass over today's
  // pending/filtered_out rows, no network call, so unlike fetch there is
  // nothing to poll; it resolves in one round trip.
  async function runMatchPhase() {
    pipeline.match.status = "running";
    setState({ progress: "Run collection · matching keywords" });
    rerenderCurrent();
    try {
      const result = await api.post("/health/pipeline/match/run");
      pipeline.match.status = "done";
      pipeline.match.detail = `${result.matched} matched, ${result.filtered_out} filtered out (of ${result.total})`;
    } catch (err) {
      pipeline.match.status = "error";
      pipeline.match.detail = fmtErr(err);
    }
    if (!alive) return;
    rerenderCurrent();
  }

  // Phase 3: the 5 LLM enrichment stages, unchanged from the previous
  // "Run all stages" implementation — one at a time via the real
  // per-stage endpoint.
  async function runEnrichPhase() {
    for (const stage of pipeline.stages) {
      if (!alive) return;
      stage.status = "running";
      stage.startedAt = Date.now();
      const doneCount = pipeline.stages.filter((s) => s.status === "done" || s.status === "error").length;
      setState({ progress: `Run collection · ${stage.name} (${doneCount}/${pipeline.stages.length})` });
      rerenderCurrent();
      try {
        const result = await api.post(`/health/pipeline/stages/${stage.key}/run`);
        stage.status = (result && result.status) || "done";
        stage.detail = (result && result.detail) || "";
      } catch (err) {
        stage.status = "error";
        stage.detail = fmtErr(err);
      }
      stage.finishedAt = Date.now();
      if (!alive) return;
      rerenderCurrent();
    }
  }

  // Fetch phase row + live per-source list. `f.sources` grows in real
  // time as GET /health/pipeline/fetch/status is polled (backed by
  // app.services.collector.run_collection's new progress_cb hook, fired
  // per source AS it completes — not batched up and revealed only once
  // the whole ~45-source run finishes). Capped to the most recent dozen
  // in a scrollable strip so a near-complete run doesn't grow the card
  // unbounded — the underlying data behind `f.completed`/`f.total` is
  // still the real count of every source, not just the ones shown.
  function fetchPhaseRowHtml(f) {
    if (!f) return "";
    const icon = f.status === "done" ? "&#10003;" : f.status === "error" ? "&#10005;" : f.status === "running" ? "&#8987;" : "&#183;";
    const color =
      f.status === "done" ? "var(--green)" : f.status === "error" ? "var(--red)" : f.status === "running" ? "var(--actx)" : "var(--tx4)";
    const detail =
      f.status === "pending"
        ? "waiting…"
        : f.status === "running"
        ? `fetching · ${f.completed}/${f.total || "?"} sources done`
        : f.status === "error"
        ? f.detail || "fetch failed"
        : `done · ${f.detail}`;
    const sourceRows = (f.sources || [])
      .slice(-12)
      .map((s) => {
        const sIcon = s.status === "ok" ? "&#10003;" : s.status === "failed" ? "&#10005;" : "&#183;";
        const sColor = s.status === "ok" ? "var(--green)" : s.status === "failed" ? "var(--red)" : "var(--tx4)";
        const sDetail = s.status === "failed" ? s.error || "failed" : `${s.fetched} fetched · ${s.inserted} new`;
        return `<div class="row" style="gap:8px;padding:2px 0;">
          <span style="width:12px;text-align:center;color:${sColor};font-size:11px;">${sIcon}</span>
          <span style="flex:0 0 auto;max-width:170px;font-size:11px;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(s.name)}</span>
          <span style="font-size:10.5px;color:var(--tx4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(sDetail)}</span>
        </div>`;
      })
      .join("");
    return `<div style="padding:6px 0;">
      <div class="row" style="gap:11px;">
        <span style="width:16px;text-align:center;color:${color};font-size:13px;">${icon}</span>
        <span style="flex:0 0 150px;font-size:12.5px;font-weight:600;">Fetch news</span>
        <span style="flex:1;min-width:0;font-size:11.5px;color:var(--tx3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(detail)}">${escapeHtml(detail)}</span>
      </div>
      ${sourceRows ? `<div style="max-height:150px;overflow-y:auto;margin:4px 0 0 27px;padding-left:9px;border-left:1px solid var(--line);">${sourceRows}</div>` : ""}
    </div>`;
  }

  function matchPhaseRowHtml(m) {
    if (!m) return "";
    const icon = m.status === "done" ? "&#10003;" : m.status === "error" ? "&#10005;" : m.status === "running" ? "&#8987;" : "&#183;";
    const color =
      m.status === "done" ? "var(--green)" : m.status === "error" ? "var(--red)" : m.status === "running" ? "var(--actx)" : "var(--tx4)";
    const detail = m.status === "pending" ? "waiting…" : m.status === "running" ? "matching…" : m.detail || m.status;
    return `<div class="row" style="gap:11px;padding:6px 0;">
      <span style="width:16px;text-align:center;color:${color};font-size:13px;">${icon}</span>
      <span style="flex:0 0 150px;font-size:12.5px;font-weight:600;">Match keywords</span>
      <span style="flex:1;min-width:0;font-size:11.5px;color:var(--tx3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(detail)}">${escapeHtml(detail)}</span>
    </div>`;
  }

  function pipelinePanelHtml() {
    if (!pipeline) return "";
    const elapsedTotal = ((pipeline.finishedAt || Date.now()) - pipeline.startedAt) / 1000;
    const hasError =
      pipeline.stages.some((s) => s.status === "error") ||
      (pipeline.fetch && pipeline.fetch.status === "error") ||
      (pipeline.match && pipeline.match.status === "error");
    const stageRows = pipeline.stages
      .map((s) => {
        const icon = s.status === "done" ? "&#10003;" : s.status === "error" ? "&#10005;" : s.status === "running" ? "&#8987;" : "&#183;";
        const color =
          s.status === "done" ? "var(--green)" : s.status === "error" ? "var(--red)" : s.status === "running" ? "var(--actx)" : "var(--tx4)";
        const elapsed = s.startedAt ? ((s.finishedAt || Date.now()) - s.startedAt) / 1000 : 0;
        const detail =
          s.status === "pending"
            ? "waiting…"
            : s.status === "running"
            ? `running · ${elapsed.toFixed(0)}s elapsed`
            : `${s.status} · ${elapsed.toFixed(1)}s${s.detail ? " · " + escapeHtml(s.detail) : ""}`;
        return `<div class="row" style="gap:11px;padding:6px 0;">
          <span style="width:16px;text-align:center;color:${color};font-size:13px;">${icon}</span>
          <span style="flex:0 0 150px;font-size:12.5px;font-weight:600;">${escapeHtml(s.name)}</span>
          <span style="flex:1;min-width:0;font-size:11.5px;color:var(--tx3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${detail}">${detail}</span>
        </div>`;
      })
      .join("");
    return `<div class="card" style="margin-bottom:16px;border-color:${pipeline.active ? "var(--ac2)" : hasError ? "var(--red)" : "var(--line)"};">
      <div class="row-between" style="margin-bottom:4px;">
        <h3 style="margin:0;">Run collection ${pipeline.active ? "· running" : hasError ? "· finished with errors" : "· done"}</h3>
        <span class="mono" style="font-size:11px;color:var(--tx3);">${elapsedTotal.toFixed(0)}s elapsed</span>
      </div>
      <p class="hint" style="margin:0 0 10px;">Fetches today's news from every enabled source, matches it against your keyword rules, then runs enrichment — pick which phases run from the checkboxes above. Progress below is real: live per-source fetch status, not an instant "done".</p>
      <div class="stack" style="gap:0;">
        ${fetchPhaseRowHtml(pipeline.fetch)}
        ${matchPhaseRowHtml(pipeline.match)}
        ${stageRows}
      </div>
    </div>`;
  }

  // Phase checkboxes for "Run collection" — disabled while a run is
  // active (the running set was already snapshotted when it started;
  // toggling mid-run only affects the NEXT click).
  function phaseCheckboxHtml(key, label, running) {
    return `<label class="row" style="gap:5px;align-items:center;font-size:11px;color:var(--tx3);cursor:${running ? "default" : "pointer"};white-space:nowrap;">
      <input type="checkbox" data-phase="${key}" ${phases[key] ? "checked" : ""} ${running ? "disabled" : ""} style="accent-color:var(--ac2);width:13px;height:13px;" />
      ${label}
    </label>`;
  }

  function header(isAdmin) {
    const running = !!(pipeline && pipeline.active);
    return `
      <div class="row wrap" style="margin-bottom:22px;">
        <div class="seg-group" style="height:auto;padding:3px;">
          ${["short", "long"]
            .map(
              (m) => `
            <button data-mode="${m}" class="seg-btn ${mode === m ? "active" : ""}" style="flex-direction:column;align-items:flex-start;gap:1px;padding:8px 16px;height:auto;">
              <span style="font-size:13px;font-weight:600;">${m === "short" ? "Short term" : "Long term"}</span>
              <span style="font-size:10.5px;opacity:.75;">${m === "short" ? "Today · 14 days" : "Economic trends · 12 months"}</span>
            </button>`
            )
            .join("")}
        </div>
        <div class="grow"></div>
        ${
          isAdmin
            ? `<div class="row" style="gap:12px;align-items:center;flex-wrap:wrap;">
                <div class="row" style="gap:11px;">
                  ${phaseCheckboxHtml("fetch", "Fetch", running)}
                  ${phaseCheckboxHtml("match", "Match", running)}
                  ${phaseCheckboxHtml("enrich", "Enrich", running)}
                </div>
                <button id="runCollectionBtn" class="btn btn-soft" style="white-space:nowrap;" ${running ? "disabled" : ""}>&#9654; ${running ? "Running…" : "Run collection"}</button>
              </div>`
            : ""
        }
        <button id="buildExportBtn" class="btn btn-outline" style="white-space:nowrap;">&#8595; Build an export</button>
      </div>
    `;
  }

  function loadingShell(m) {
    const isAdmin = !!(state.user && state.user.is_admin);
    if (m === "short") {
      return (
        header(isAdmin) +
        `<div class="grid-4" style="margin-bottom:16px;">${Array.from({ length: 4 })
          .map(() => `<div class="card card-sm">${skeletonRows(3)}</div>`)
          .join("")}</div>
        <div class="grid-2" style="margin-bottom:16px;">
          <div class="card">${skeletonRows(4, 20)}</div>
          <div class="card">${skeletonRows(6, 20)}</div>
        </div>`
      );
    }
    return (
      header(isAdmin) +
      `<div class="grid-3" style="margin-bottom:16px;">${Array.from({ length: 3 })
        .map(() => `<div class="card card-sm">${skeletonRows(3)}</div>`)
        .join("")}</div>`
    );
  }

  function renderError(err) {
    const isAdmin = !!(state.user && state.user.is_admin);
    const label = mode === "short" ? "GET /dashboard/today" : "GET /dashboard/trends";
    const body =
      err && err.kind === "not_found"
        ? notWiredBanner(label)
        : `<div class="banner banner-error"><span>&#10005;</span><div style="flex:1">${escapeHtml(fmtErr(err))}</div><button id="dashRetry" class="btn-ghost" style="color:var(--red);text-decoration:underline;">Retry</button></div>`;
    render(header(isAdmin) + body);
    const retry = root.querySelector("#dashRetry");
    if (retry) retry.addEventListener("click", load);
  }

  // ── Short term ──────────────────────────────────────────────────
  function renderShort(data) {
    const isAdmin = !!(state.user && state.user.is_admin);
    if (data.pendingCount !== undefined) setState({ pendingCount: data.pendingCount, approvedCount: data.approvedCount });

    const kpis = data.kpis || [];
    const kpiColors = ["var(--tx)", "var(--tx)", "var(--actx)", "var(--green)"];
    const kpisHtml = kpis.length
      ? kpis
          .map(
            (k, i) => `
        <div class="card card-sm kpi">
          <div class="kpi-value-row">
            <div class="kpi-value" style="color:${kpiColors[i] || "var(--tx)"}">${escapeHtml(k.value)}</div>
            ${k.delta ? `<div class="kpi-delta" style="color:${deltaColor(k.delta)}">${escapeHtml(k.delta)}</div>` : ""}
          </div>
          <div class="kpi-label">${escapeHtml(k.label)}</div>
          ${kpiSparklineHtml(k.vals)}
          <div class="kpi-note">${escapeHtml(k.note || "")}</div>
        </div>`
          )
          .join("")
      : `<div class="card card-sm"><div class="state-body" style="margin:0;">No KPI data returned.</div></div>`.repeat(1);

    const funnel = data.funnel || [];
    const funnelFills = ["var(--tx4)", "var(--ac)", "var(--ac3)", "var(--green)"];
    const phase1 = funnel[0] ? Number(String(funnel[0].value).replace(/,/g, "")) || 1 : 1;
    const funnelHtml = funnel.length
      ? funnel
          .map((f, i) => {
            const v = Number(String(f.value).replace(/,/g, "")) || 0;
            const linear = phase1 ? v / phase1 : 0;
            const width = Math.max(6, Math.round(Math.sqrt(linear) * 100)) + "%";
            return `
        <div>
          <div class="row-between" style="margin-bottom:6px;">
            <div class="row" style="gap:9px;">
              <span class="mono" style="font-size:9.5px;color:var(--tx4);">${escapeHtml(f.phase || "Phase " + (i + 1))}</span>
              <span style="font-size:13px;font-weight:600;">${escapeHtml(f.label)}</span>
            </div>
            <div class="row" style="gap:8px;">
              <span style="font-size:15px;font-weight:700;letter-spacing:-.4px;">${escapeHtml(f.value)}</span>
              <span class="mono" style="font-size:10.5px;color:var(--tx3);">${escapeHtml(f.pct)}</span>
            </div>
          </div>
          <div class="bar-track"><div class="bar-fill" style="width:${width};background:${funnelFills[i] || "var(--ac)"}"></div></div>
          <div class="kpi-note">${escapeHtml(f.note || "")}</div>
        </div>`;
          })
          .join("")
      : `<div class="state-body">No funnel data returned.</div>`;

    const days = (data.days14 || []).map((d) => {
      const dt = d.date ? new Date(d.date + "T00:00:00") : null;
      const isToday = dt ? sameDay(dt, new Date()) : false;
      const isWeekend = dt ? dt.getDay() === 0 || dt.getDay() === 6 : false;
      return { ...d, dt, isToday, isWeekend };
    });
    const maxDay = days.reduce((m, d) => Math.max(m, d.count || 0), 0) || 1;
    const daysHtml = days.length
      ? days
          .map((d) => {
            const height = Math.round(((d.count || 0) / maxDay) * 100) + "%";
            const fill = d.isToday ? "var(--ac3)" : d.isWeekend ? "var(--line2)" : "var(--ac)";
            const letter = d.dt ? DAY_LETTERS[d.dt.getDay()] : "·";
            return `<div class="chart-col" title="${escapeHtml(d.count)} matched">
              <div class="chart-bar" style="height:${height};background:${fill}"></div>
              <span class="chart-daylabel">${letter}</span>
            </div>`;
          })
          .join("")
      : "";
    const avg = days.length ? Math.round(days.reduce((s, d) => s + (d.count || 0), 0) / days.length) : "—";
    const peak = days.length ? Math.max(...days.map((d) => d.count || 0)) : "—";
    const fetchedTotal = kpis[0] ? Number(String(kpis[0].value).replace(/,/g, "")) : 0;
    const matchTotal = days.reduce((s, d) => s + (d.count || 0), 0);
    const matchRate = fetchedTotal ? ((matchTotal / days.length / fetchedTotal) * 100).toFixed(1) + "%" : "—";

    const relRaw = data.relMix || [];
    const relTotal = relRaw.reduce((s, r) => s + (r.count || 0), 0);
    const relHtml = relRaw
      .map((r) => {
        const meta = REL_TIER_META[r.key] || {};
        return { ...meta, key: r.key, count: r.count, width: pctOfTotal(r.count, relTotal) };
      })
      .map(
        (r) => `<div style="width:${r.width};background:${r.color}" title="${escapeHtml(r.label)} · ${r.count} articles"></div>`
      )
      .join("");
    const relRowsHtml = relRaw
      .map((r) => REL_TIER_META[r.key] && { ...REL_TIER_META[r.key], count: r.count })
      .filter(Boolean)
      .map(
        (r) => `<div class="row" style="gap:10px;">
          <span style="width:8px;height:8px;flex:0 0 8px;border-radius:50%;background:${r.color}"></span>
          <span style="flex:1;font-size:12.5px;color:var(--tx2);">${r.label}</span>
          <span class="mono" style="font-size:11px;color:var(--tx3);">${r.band}</span>
          <span style="font-size:13px;font-weight:600;min-width:26px;text-align:right;">${r.count}</span>
        </div>`
      )
      .join("");

    const bySection = data.bySection || [];
    const bySectionHtml = bySection
      .map(
        (s) => `<div>
          <div class="row-between" style="margin-bottom:6px;"><span style="font-size:12.5px;color:var(--tx2);">${escapeHtml(
            s.label
          )}</span><span style="font-size:12.5px;font-weight:600;">${s.count}</span></div>
          <div class="bar-track bar-track-sm"><div class="bar-fill" style="width:${pctOfMax(
            s.count,
            bySection,
            (x) => x.count
          )};background:var(--ac2);"></div></div>
        </div>`
      )
      .join("");

    const topSources = data.topSources || [];
    const topSourcesHtml = topSources
      .map(
        (s, i) => `<div>
          <div class="row-between" style="margin-bottom:6px;"><span style="font-size:12.5px;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(
            s.label
          )}</span><span style="font-size:12.5px;font-weight:600;flex:0 0 auto;padding-left:8px;">${s.count}</span></div>
          <div class="bar-track bar-track-sm"><div class="bar-fill" style="width:${pctOfMax(
            s.count,
            topSources,
            (x) => x.count
          )};background:${i === 0 ? "var(--ac3)" : "var(--ac)"};"></div></div>
        </div>`
      )
      .join("");

    render(`
      ${header(isAdmin)}
      ${pipelinePanelHtml()}
      <div class="grid-4" style="margin-bottom:16px;">${kpisHtml}</div>
      <div class="grid-2" style="margin-bottom:16px;">
        <div class="card">
          <h3>Today's funnel</h3>
          <p class="hint">How fetched articles become a reviewable queue.</p>
          <div class="stack" style="gap:13px;">${funnelHtml}</div>
        </div>
        <div class="card">
          <h3>Matched volume · last 14 days</h3>
          <p class="hint">Keyword-matched articles per day. Weekends run light.</p>
          <div class="chart-cols">${daysHtml || `<div class="state-body">No data.</div>`}</div>
          <div class="chart-summary">
            <div><div class="num">${avg}</div><div class="lbl">14-day avg</div></div>
            <div><div class="num">${peak}</div><div class="lbl">Peak</div></div>
            <div><div class="num">${matchRate}</div><div class="lbl">Match rate</div></div>
          </div>
        </div>
      </div>
      <div class="grid-3">
        <div class="card">
          <h3 style="margin-bottom:18px;">Relevance mix</h3>
          <div style="display:flex;height:11px;border-radius:6px;overflow:hidden;margin-bottom:18px;">${relHtml}</div>
          <div class="stack" style="gap:11px;">${relRowsHtml}</div>
        </div>
        <div class="card">
          <h3 style="margin-bottom:18px;">By section</h3>
          <div class="stack" style="gap:14px;">${bySectionHtml || `<div class="state-body">No data.</div>`}</div>
        </div>
        <div class="card">
          <h3 style="margin-bottom:18px;">Most productive sources</h3>
          <div class="stack" style="gap:14px;">${topSourcesHtml || `<div class="state-body">No data.</div>`}</div>
        </div>
      </div>
    `);
  }

  // ── Long term ───────────────────────────────────────────────────
  function renderLong(data) {
    const isAdmin = !!(state.user && state.user.is_admin);
    // MACRO-DASHBOARD: each card now also carries where its CURRENT
    // value came from this request -- TrendCardOut.status
    // ("live"/"manual"/"unconfigured"/"rollup", app/api/schemas/core.py)
    // plus an optional asOf timestamp and a human note. Surfaced as a
    // small badge + caption under the sparkline so staleness/"not real
    // data yet" is visible rather than hidden behind a plain number.
    const trendCards = (data.trendCards || []).map((t) => {
      const vals = t.vals || [];
      const first = vals[0] ?? 0;
      const last = vals[vals.length - 1] ?? 0;
      const pc = first ? ((last - first) / first) * 100 : 0;
      const rising = pc >= 0;
      const bad = !!t.bad;
      const badDirection = bad ? rising : !rising;
      const decimals = t.decimals !== undefined ? t.decimals : 1;
      return {
        label: t.label,
        unit: t.unit,
        value: last.toFixed(decimals),
        rawValue: last,
        delta: (rising ? "▲ " : "▼ ") + Math.abs(pc).toFixed(1) + "%",
        color: badDirection ? "var(--red)" : "var(--green)",
        fillColor: badDirection ? "var(--redsoft)" : "var(--greensoft)",
        points: poly(vals, 240, 56, false),
        area: poly(vals, 240, 56, true),
        status: t.status || "rollup",
        asOf: t.asOf || "",
        note: t.note || "",
      };
    });
    const trendHtml = trendCards.length
      ? trendCards
          .map(
            (t) => `<div class="card card-sm">
        <div class="trend-card-head">
          <div class="trend-card-label">${escapeHtml(t.label)}</div>
          <div class="trend-card-delta" style="color:${t.color}">${escapeHtml(t.delta)}</div>
        </div>
        <div class="row" style="align-items:baseline;gap:6px;">
          <div class="trend-card-value">${escapeHtml(t.value)}</div>
          <div class="trend-card-unit">${escapeHtml(t.unit)}</div>
        </div>
        <svg viewBox="0 0 240 56" preserveAspectRatio="none" style="width:100%;height:48px;margin-top:14px;display:block;overflow:visible;">
          <polyline points="${t.area}" fill="${t.fillColor}" stroke="none"></polyline>
          <polyline points="${t.points}" fill="none" stroke="${t.color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"></polyline>
        </svg>
        ${macroStatusHtml(t)}
      </div>`
          )
          .join("")
      : `<div class="card card-sm"><div class="state-body">No trend data returned.</div></div>`;

    const months = data.months12 || [];
    const monthTotals = months.map((m) => Object.values(m.segments || {}).reduce((a, b) => a + b, 0));
    const monthMax = Math.max(1, ...monthTotals);
    // Union of every section actually present in the 12 months of data,
    // ordered by RISK_SECTIONS where possible (stable legend order) with
    // any section RISK_SECTIONS doesn't know about yet appended after —
    // so a brand-new admin-created section still renders/stacks/colors
    // correctly even if the frontend's cached section list (util.js's
    // ensureRiskSections()) hasn't caught up yet. This is the long-term
    // chart's own half of the hardcoded-sections fix: RISK_SECTIONS used
    // to be the ONLY source for which sections this chart would ever draw.
    const seenSections = new Set();
    months.forEach((m) => Object.keys(m.segments || {}).forEach((k) => seenSections.add(k)));
    const secOrder = [...RISK_SECTIONS.filter((k) => seenSections.has(k)), ...[...seenSections].filter((k) => !RISK_SECTIONS.includes(k))];
    const monthsHtml = months
      .map((m, i) => {
        const total = monthTotals[i] || 0;
        const segs = secOrder
          .map((k) => {
            const v = (m.segments || {})[k] || 0;
            const h = total ? (v / total) * 100 : 0;
            return `<div style="height:${h}%;background:${sectionColor(k)}"></div>`;
          })
          .join("");
        return `<div class="months-col">
          <div class="months-stack" style="height:${Math.round((total / monthMax) * 100)}%;" title="${escapeHtml(m.label)} · ${total} matched">${segs}</div>
          <span class="chart-daylabel">${escapeHtml(m.label)}</span>
        </div>`;
      })
      .join("");
    const legendHtml = secOrder
      .map((k) => `<div class="row" style="gap:7px;"><span style="width:8px;height:8px;border-radius:2px;background:${sectionColor(k)}"></span><span style="font-size:11.5px;color:var(--tx2);">${escapeHtml(k)}</span></div>`)
      .join("");

    // Relevance-mix trend (dashboard-longterm-spec.md item 2): monthly
    // share of high-scoring (>=8) matched coverage. GLOBAL, not
    // per-section — see RelMixTrendPointOut's docstring
    // (app/api/schemas/core.py) for the rollup-schema gap that makes a
    // per-section cut unavailable this round.
    const relMixTrend = data.relMixTrend || [];
    const relMixHtml = relMixTrend
      .map((p) => {
        const sharePct = Math.round((p.highShare || 0) * 100);
        return `<div class="chart-col" title="${escapeHtml(p.label)} · ${sharePct}% high of ${p.total} matched">
          <div class="chart-bar" style="height:${sharePct}%;background:var(--red);"></div>
          <span class="chart-daylabel">${escapeHtml(p.label)}</span>
        </div>`;
      })
      .join("");

    // Top sources, 12 months (spec item 5) — aggregate across the whole
    // window (rollup_monthly_source has no section column; see
    // DashboardTrendsOut.topSources's docstring).
    const topSources12 = data.topSources || [];
    const topSources12Html = topSources12
      .map(
        (s, i) => `<div>
          <div class="row-between" style="margin-bottom:6px;"><span style="font-size:12.5px;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(
            s.label
          )}</span><span style="font-size:12.5px;font-weight:600;flex:0 0 auto;padding-left:8px;">${s.count}</span></div>
          <div class="bar-track bar-track-sm"><div class="bar-fill" style="width:${pctOfMax(
            s.count,
            topSources12,
            (x) => x.count
          )};background:${i === 0 ? "var(--ac3)" : "var(--ac)"};"></div></div>
        </div>`
      )
      .join("");

    const heatRows = data.heatRows || [];
    const heatHtml = heatRows
      .map((h) => {
        const vals = h.values || [];
        const trend = h.trend || autoTrend(vals);
        const trendColor = trend === "▲" ? "var(--red)" : trend === "▼" ? "var(--green)" : "var(--tx3)";
        const cells = vals
          .map((v) => `<div class="heat-cell" style="background:color-mix(in oklab, var(--ac2) ${Math.round(v * 100)}%, var(--gridline));" title="${Math.round(v * 100)}% of peak"></div>`)
          .join("");
        return `<div class="heat-row">
          <span class="heat-label">${escapeHtml(h.label)}</span>
          <div class="heat-cells">${cells}</div>
          <span class="heat-trend" style="color:${trendColor}">${trend}</span>
        </div>`;
      })
      .join("");

    const standing = data.standing || [];
    const standingHtml = standing.length
      ? standing
          .map((t) => {
            const width = pctOfMax(t.count, standing, (x) => x.count);
            const fill = t.dir === "up" ? "var(--red)" : t.dir === "down" ? "var(--green)" : "var(--ac2)";
            const badgeStrong = (t.months || 0) >= 9;
            return `<div class="standing-card">
          <div class="row-between" style="align-items:flex-start;">
            <div class="standing-name">${escapeHtml(t.name)}</div>
            <span class="pill" style="background:${badgeStrong ? "var(--acsoft)" : "var(--gridline)"};color:${badgeStrong ? "var(--actx)" : "var(--tx3)"}">${t.months} mo</span>
          </div>
          <div class="standing-note">${escapeHtml(t.note)}</div>
          <div class="row" style="gap:10px;margin-top:12px;">
            <div class="bar-track" style="height:5px;flex:1;"><div class="bar-fill" style="width:${width};background:${fill};height:100%;"></div></div>
            <span class="mono" style="font-size:10px;color:var(--tx3);">${t.count} arts</span>
          </div>
        </div>`;
          })
          .join("")
      : `<div class="state-body">No standing-theme data returned.</div>`;

    render(`
      ${header(isAdmin)}
      ${pipelinePanelHtml()}
      <div class="grid-3" style="margin-bottom:16px;">${trendHtml}</div>
      <div class="card" style="margin-bottom:16px;">
        <h3>Coverage volume by section · 12 months</h3>
        <p class="hint">Matched articles per month, stacked by section — this is the competitor/regulatory/economic tracker once sections are configured (Keywords admin screen). The primary chart on this tab.</p>
        <div class="months-cols">${monthsHtml || `<div class="state-body">No data.</div>`}</div>
        <div class="row wrap" style="gap:16px;margin-top:18px;padding-top:14px;border-top:1px solid var(--line);">${legendHtml}</div>
      </div>
      <div class="grid-2-long" style="margin-bottom:16px;">
        <div class="card">
          <h3>Relevance mix · 12 months</h3>
          <p class="hint">Share of matched coverage scoring high (8–10) each month, across all sections.</p>
          <div class="chart-cols">${relMixHtml || `<div class="state-body">No data.</div>`}</div>
        </div>
        <div class="card">
          <h3>Theme intensity</h3>
          <p class="hint">How often each risk theme surfaced, month by month.</p>
          <div class="stack" style="gap:7px;">${heatHtml || `<div class="state-body">No data.</div>`}</div>
        </div>
      </div>
      <div class="grid-2" style="margin-bottom:16px;">
        <div class="card">
          <h3>Standing themes</h3>
          <p class="hint">Themes that have run for three months or more.</p>
          <div class="grid-2">${standingHtml}</div>
        </div>
        <div class="card">
          <h3 style="margin-bottom:18px;">Top sources · 12 months</h3>
          <div class="stack" style="gap:14px;">${topSources12Html || `<div class="state-body">No data.</div>`}</div>
        </div>
      </div>
    `);
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }
  function autoTrend(vals) {
    if (!vals.length) return "▬";
    const d = vals[vals.length - 1] - vals[0];
    if (Math.abs(d) < 0.05) return "▬";
    return d > 0 ? "▲" : "▼";
  }

  return {
    destroy() {
      alive = false;
      stopTicking();
    },
  };
}

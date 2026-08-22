// Dashboard — Short term + Long term (frontend-contract.md §3.1/§3.2).
//
// SHORT TERM is rendered here, from one endpoint
// (app/api/routers/dashboard.py):
//
//   GET /dashboard/today ->
//     { pendingCount, approvedCount, todayLabel,
//       kpis: [{ value, label, delta, note, vals: [14 real daily counts, may be empty] }, ...4],
//       funnel: [{ phase, label, value, pct, note }, ...4],
//       days14: [{ date: "YYYY-MM-DD", count }, ...14],
//       relMix: [{ key: "red"|"yellow"|"green"|"magenta", count }, ...4],
//       bySection: [{ label, count }, ...],  // one row per REAL configured section (get_risk_sections)
//       topSources: [{ label, count }, ...] }
//
// LONG TERM is four sub-tabs and lives in ./dashboard_long.js, one
// endpoint per tab. It used to be a single GET /dashboard/trends payload
// rendered here; that endpoint still exists and still serves the old
// shape, but nothing on this screen reads it any more. Three of the four
// tabs are backed by data it never carried — real exchange history, the
// economy axes, and the individual headlines behind every count — and a
// reader looks at one tab at a time, so opening Risk lines should not
// pay for the market fetch.
//
// Bar-width / heatmap-cell scaling is computed from the REAL maximum in
// the returned set (util.pctOfMax), never a hardcoded denominator.

import { api } from "../api.js";
import { bindLongTab, fetchLongTab, longTabsHtml, renderLongTab } from "./dashboard_long.js";
import { setState, state, subscribe } from "../state.js";
import {
  escapeHtml,
  pctOfMax,
  pctOfTotal,
  poly,
  ensureRiskSections,
  fmtErr,
  skeletonRows,
  notWiredBanner,
} from "../util.js";

const REL_TIER_META = {
  red: { label: "High", band: "8 to 10", color: "var(--red)" },
  yellow: { label: "Medium", band: "6 to 7", color: "var(--amber)" },
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

export function mountDashboard(root, { navigate }) {
  let alive = true;
  const mode = state.dashMode || "short";
  let lastData = null; // cached last-loaded payload (short-term tab)
  let loadedLongTab = null; // which long sub-tab the DOM currently shows
  let unsubscribe = null;
  ensureRiskSections();

  if (mode === "long") {
    loadLong();
    // shell.js does not remount on a longTab change, by design, so the
    // sub-tabs are this mount's job to follow.
    // `state.screen` is checked as well as the mode. setState() notifies
    // listeners BEFORE shell.js's syncScreen has had a chance to destroy
    // this mount, so a navigation AWAY from the dashboard reaches this
    // listener while the mount is still alive. Without the screen check
    // it starts a fetch for a screen the user has just left, and the
    // response paints the dashboard over whatever mounted in its place.
    unsubscribe = subscribe(() => {
      if (alive && state.screen === "dash" && (state.dashMode || "short") === "long") loadLong();
    });
  } else {
    render(loadingShell(mode));
    load();
  }

  async function load() {
    try {
      const data = await api.get("/dashboard/today");
      if (!alive) return;
      lastData = data || {};
      renderShort(lastData);
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
      // Through navigate(), not setState: the Short/Long tab is part of
      // the address (/dash vs /dash/long), so switching it has to move
      // the URL too or the tab would silently disagree with the bar.
      btn.addEventListener("click", () => navigate("dash", { dashMode: btn.getAttribute("data-mode") }));
    });
    const goRunBtn = root.querySelector("#goRunBtn");
    if (goRunBtn) goRunBtn.addEventListener("click", () => navigate("run"));
    const buildExportBtn = root.querySelector("#buildExportBtn");
    if (buildExportBtn) buildExportBtn.addEventListener("click", () => navigate("export"));
    const dashRetry = root.querySelector("#dashRetry");
    if (dashRetry) dashRetry.addEventListener("click", () => load());
  }

  // Running a collection lives on its own admin screen now
  // (screens/run.js), not here. It is admin-only and global, it needs
  // room for the full ~45-source progress list, and its polling has to
  // patch the DOM in place rather than re-render — none of which fits a
  // per-day reading dashboard. This header just links to it.
  function header(isAdmin) {
    return `
      <div class="row wrap" style="margin-bottom:22px;">
        <div class="seg-group" style="height:auto;padding:3px;">
          ${["short", "long"]
            .map(
              (m) => `
            <button data-mode="${m}" class="seg-btn ${mode === m ? "active" : ""}" style="flex-direction:column;align-items:flex-start;gap:1px;padding:8px 16px;height:auto;">
              <span style="font-size:13px;font-weight:600;">${m === "short" ? "Short term" : "Long term"}</span>
              <span style="font-size:10.5px;opacity:.75;">${m === "short" ? "Today · 14 days" : "Risk lines · 12 months"}</span>
            </button>`
            )
            .join("")}
        </div>
        <div class="grow"></div>
        ${
          isAdmin
            ? `<button id="goRunBtn" class="btn btn-soft" style="white-space:nowrap;">&#9654; Run collection</button>`
            : ""
        }
        <button id="buildExportBtn" class="btn btn-outline" style="white-space:nowrap;">&#8595; Build an export</button>
      </div>
    `;
  }

  // Short-term only. The long-term tabs render their own skeleton from
  // loadLong(), because each of the four has a different shape.
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
    // Phase 1 is the denominator for every bar below it.
    //
    // This used to read `Number(...) || 1`, meant as a divide-by-zero
    // guard. But 0 is falsy, so on a day before the first run the
    // denominator became 1 and "Approved for export: 5" computed
    // sqrt(5/1) * 100 = 224% — a bar overflowing its track, sitting
    // next to a label reading "0%". Those 5 approvals are of articles
    // fetched on EARLIER days, so they are not a fraction of today's
    // fetch at all; the honest drawing is an empty funnel.
    const phase1 = funnel[0] ? Number(String(funnel[0].value).replace(/,/g, "")) || 0 : 0;
    const funnelHtml = funnel.length
      ? funnel
          .map((f, i) => {
            const v = Number(String(f.value).replace(/,/g, "")) || 0;
            // sqrt keeps the small stages visible in a funnel whose top
            // is thousands and whose bottom is single digits. Clamped,
            // because a ratio above 1 is a bug rather than a wide bar,
            // and a genuine zero draws nothing rather than a stub that
            // reads as "some".
            const linear = phase1 > 0 ? Math.min(1, v / phase1) : 0;
            const width = (v > 0 && phase1 > 0 ? Math.max(6, Math.round(Math.sqrt(linear) * 100)) : 0) + "%";
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
    const avg = days.length ? Math.round(days.reduce((s, d) => s + (d.count || 0), 0) / days.length) : "n/a";
    const peak = days.length ? Math.max(...days.map((d) => d.count || 0)) : "n/a";
    const fetchedTotal = kpis[0] ? Number(String(kpis[0].value).replace(/,/g, "")) : 0;
    // TODAY's matched / TODAY's fetched.
    //
    // This used to be `matchTotal / days.length / fetchedTotal`, which
    // divided the 14-DAY matched total by 14 and then by TODAY's fetched
    // count — an average-day numerator over a single-day denominator.
    // The two are not comparable, so the number it produced was not a
    // rate of anything: on a light day it read far over 100%, and on a
    // day with a big fetch it read near zero.
    const matchedToday = kpis[1] ? Number(String(kpis[1].value).replace(/,/g, "")) : 0;
    const matchRate = fetchedTotal ? ((matchedToday / fetchedTotal) * 100).toFixed(1) + "%" : "n/a";

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
  // ── Long term ───────────────────────────────────────────────────
  // Four sub-tabs, one endpoint each. All rendering lives in
  // screens/dashboard_long.js; this function only orchestrates fetch,
  // render and rebind, and owns the shell (header, loading, errors) that
  // the Short-term tab shares.
  //
  // Switching sub-tabs does NOT remount the screen — shell.js
  // deliberately excludes longTab from its remount comparison — so this
  // mount watches state and re-fetches itself when the tab changes.
  // Without that the address bar would move and the screen would not.
  function renderLongShell(tab, inner) {
    render(
      header(!!(state.user && state.user.is_admin)) +
        longTabsHtml(tab) +
        `<div class="stack" style="margin-top:18px;">${inner}</div>`
    );
    bindLongTab(root, tab, { navigate, reload: () => loadLong(true) });
  }

  async function loadLong(force = false) {
    const tab = state.longTab || "risk";
    if (!force && tab === loadedLongTab) return;
    loadedLongTab = tab;
    renderLongShell(tab, `<div class="card">${skeletonRows(6)}</div>`);
    try {
      const data = await fetchLongTab(tab);
      // The user may have switched tabs — or left the dashboard entirely —
      // while this was in flight. Render only if the answer still matches
      // what the screen is showing.
      if (!alive || state.screen !== "dash" || (state.longTab || "risk") !== tab) return;
      renderLongShell(tab, renderLongTab(tab, data));
    } catch (err) {
      if (!alive || state.screen !== "dash" || (state.longTab || "risk") !== tab) return;
      renderLongShell(
        tab,
        `<div class="card">
           <div class="banner banner-error"><div>
             <div style="font-weight:600;margin-bottom:4px;">This tab could not load.</div>
             <div style="font-size:12.5px;">${escapeHtml(fmtErr(err))}</div>
           </div></div>
           <button type="button" id="longRetry" class="btn btn-outline">Try again</button>
         </div>`
      );
      root.querySelector("#longRetry")?.addEventListener("click", () => loadLong(true));
    }
  }

  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }
  return {
    destroy() {
      alive = false;
      if (unsubscribe) unsubscribe();
    },
  };
}

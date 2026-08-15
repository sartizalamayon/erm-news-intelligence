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

export function mountDashboard(root, { navigate }) {
  let alive = true;
  const mode = state.dashMode || "short";
  let lastData = null; // cached last-loaded payload
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
              <span style="font-size:10.5px;opacity:.75;">${m === "short" ? "Today · 14 days" : "Economic trends · 12 months"}</span>
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
      <div class="grid-3" style="margin-bottom:16px;">${trendHtml}</div>
      <div class="card" style="margin-bottom:16px;">
        <h3>Coverage volume by section · 12 months</h3>
        <p class="hint">Matched articles per month, stacked by section. This is the competitor, regulatory and economic tracker once sections are configured (Keywords admin screen). The primary chart on this tab.</p>
        <div class="months-cols">${monthsHtml || `<div class="state-body">No data.</div>`}</div>
        <div class="row wrap" style="gap:16px;margin-top:18px;padding-top:14px;border-top:1px solid var(--line);">${legendHtml}</div>
      </div>
      <div class="grid-2-long" style="margin-bottom:16px;">
        <div class="card">
          <h3>Relevance mix · 12 months</h3>
          <p class="hint">Share of matched coverage scoring high (8 to 10) each month, across all sections.</p>
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
    },
  };
}

// Long-term dashboard — the four sub-tabs under Dashboard > Long term.
// Rendering only; `screens/dashboard.js` owns the screen shell (the
// Short/Long segmented control, the Run-collection and Build-an-export
// buttons, error and loading states) and delegates here whenever
// state.dashMode is "long".
//
// Backed by four endpoints, one per tab (app/api/routers/dashboard.py,
// models in app/api/schemas/dashboard_long.py):
//
//   GET /dashboard/long/risk     -> { lines: [RiskLineOut], moved: [EvidenceOut], provenance }
//   GET /dashboard/long/market   -> { instruments, labels, series, months, tradingDays, asOf, window, note }
//   GET /dashboard/long/economy  -> { macro: [MacroCardOut], board: BoardOut }
//   GET /dashboard/long/telecom  -> { peers, wallets, regulatory, initiatives, watchlist }
//
// WHY FOUR ENDPOINTS AND NOT ONE. The old long-term view was a single
// GET /dashboard/trends carrying everything, which was affordable while
// everything came from the monthly rollup tables. Three of these tabs
// are backed by data that payload never had — real exchange history, the
// economy axes, and the individual headlines behind every count — and a
// reader looks at one tab at a time. One endpoint per tab means opening
// Risk lines does not pay for the market fetch.
//
// TWO WINDOWS, NEVER MIXED. Market and macro figures run a genuine
// twelve months; news-derived figures reach only as far as collection
// did. Every board and card prints its own window chip rather than the
// screen carrying one global caveat, because a number lifted into a
// board paper has to carry its span with it.
//
// EXPANSION IS CLIENT-SIDE. Each tab payload already contains up to
// three critical items per row, so opening a row is a local toggle with
// no second request and no loading state to get wrong. `criticalTruncated`
// tells the row to say "3 of 172 shown" rather than implying it listed
// everything.

import { api } from "../api.js";
import { escapeHtml, formatNumber } from "../util.js";

export const LONG_TAB_LABELS = [
  ["risk", "Risk lines"],
  ["market", "Market"],
  ["economy", "Economy & companies"],
  ["telecom", "Telecom watch"],
];

const TAB_PATHS = {
  risk: "/dashboard/long/risk",
  market: "/dashboard/long/market",
  economy: "/dashboard/long/economy",
  telecom: "/dashboard/long/telecom",
};

/** Fetch one tab's payload. Throws ApiError, which the caller renders. */
export function fetchLongTab(tab) {
  return api.get(TAB_PATHS[tab] || TAB_PATHS.risk);
}

// ── small shared pieces ──────────────────────────────────────────────

const TONE_CLASS = { bad: "dir-bad", warn: "dir-warn", ok: "dir-ok", none: "dir-none" };

function toneClass(tone) {
  return TONE_CLASS[tone] || "dir-none";
}

/** Arrow glyph for a direction, drawn as SVG so it scales and recolors. */
function dirIcon(direction) {
  if (direction === "rising" || direction === "elevated") {
    return `<svg width="8" height="8" viewBox="0 0 10 10" fill="currentColor" aria-hidden="true"><path d="M5 1 9 8H1z"></path></svg>`;
  }
  if (direction === "weakening" || direction === "easing") {
    return `<svg width="8" height="8" viewBox="0 0 10 10" fill="currentColor" aria-hidden="true"><path d="M5 9 1 2h8z"></path></svg>`;
  }
  if (direction === "flat") {
    return `<svg width="8" height="8" viewBox="0 0 10 10" fill="currentColor" aria-hidden="true"><rect x="1" y="4" width="8" height="2"></rect></svg>`;
  }
  return "";
}

function winChip(text, kind) {
  const cls = kind === "market" ? "win-chip win-chip-ext" : "win-chip";
  return `<span class="${cls}">${escapeHtml(text)}</span>`;
}

/**
 * One article behind a number. `reason` is the model's own recorded
 * justification, printed verbatim — when it is missing we say so rather
 * than writing a sentence that would look like the model's.
 */
function evidenceRow(item, { small = false } = {}) {
  const band = String(item.band || "magenta").toLowerCase();
  const reason = item.reason
    ? `<div class="ev-reason">${escapeHtml(item.reason)}</div>`
    : `<div class="ev-reason ev-reason-missing">No reason was recorded with this score.</div>`;
  const meta = [item.source, item.day].filter(Boolean).map(escapeHtml).join(" &middot; ");
  return `
    <div class="ev-row${small ? " ev-row-sm" : ""}">
      <div style="display:flex;flex-direction:column;align-items:center;gap:5px;">
        <div class="score-chip${small ? " score-chip-sm" : ""} band-${band}">${escapeHtml(String(item.score))}</div>
        <div class="score-band band-${band}-tx">${escapeHtml(band)}</div>
      </div>
      <div style="min-width:0;">
        <div class="ev-title">${escapeHtml(item.title || "")}</div>
        <div class="ev-meta">${meta}</div>
        ${reason}
      </div>
      <div class="ev-actions" style="display:flex;align-items:flex-start;justify-content:flex-end;">
        <button type="button" class="btn-sm js-open-review" data-article="${escapeHtml(String(item.articleId))}"
                style="display:inline-flex;align-items:center;gap:6px;">
          <svg width="11" height="11" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.7"
               stroke-linecap="round" aria-hidden="true"><path d="M3.5 5h13M3.5 10h13M3.5 15h9"></path></svg>
          Open in review
        </button>
      </div>
    </div>`;
}

function evidenceCard(title, blurb, chip, chipKind, items) {
  const body = items.length
    ? items.map((i) => evidenceRow(i)).join("")
    : `<div class="state-block" style="padding:28px 12px;"><div class="state-body" style="margin:0;">
         Nothing in this window scored high enough to list here.</div></div>`;
  return `
    <div class="card">
      <div class="row-between" style="align-items:flex-start;">
        <div style="min-width:0;">
          <h3>${escapeHtml(title)}</h3>
          <p class="hint" style="margin-bottom:0;max-width:700px;">${escapeHtml(blurb)}</p>
        </div>
        ${winChip(chip, chipKind)}
      </div>
      <div style="margin-top:16px;display:flex;flex-direction:column;">${body}</div>
    </div>`;
}

// ── the board, shared by Telecom watch and Economy ───────────────────

/**
 * Colour band for a mean severity. Fixed thresholds, NOT relative to the
 * month's maximum: an 8.7 has to look the same in a quiet month as in a
 * loud one, or the register cannot be compared across quarters.
 */
function meanColor(mean) {
  if (mean === null || mean === undefined) return "var(--tx4)";
  if (mean >= 8) return "var(--red)";
  if (mean >= 6.5) return "var(--amber)";
  if (mean >= 4) return "var(--tx3)";
  return "var(--tx4)";
}

function boardRow(row, boardKey) {
  const hasMean = row.mean !== null && row.mean !== undefined;
  const color = meanColor(row.mean);
  const bar = hasMean
    ? `<div class="bar-track bar-track-sm"><div class="bar-fill" style="width:${Math.min(100, row.mean * 10)}%;background:${color};"></div></div>`
    : `<div class="hatch" title="Nothing in this row has been scored yet"></div>`;

  const dotCls = row.isUs ? "board-dot board-dot-us" : row.isNew ? "board-dot board-dot-new" : "board-dot";
  const canOpen = row.critical > 0 && (row.crit || []).length > 0;
  const critCell = canOpen
    ? `<button type="button" class="crit-btn js-crit" data-board="${escapeHtml(boardKey)}" data-key="${escapeHtml(row.key)}"
               aria-expanded="false" aria-controls="exp-${escapeHtml(boardKey)}-${escapeHtml(row.key)}">${row.critical}</button>`
    : `<button type="button" class="crit-btn" disabled aria-label="No critical items to open">${row.critical}</button>`;

  // A row with critical items on the books whose articles have aged past
  // the raw-retention window: the count is still true, the headlines are
  // simply gone. That is a different thing from having nothing to show.
  const expiredNote = row.critExpired
    ? `<div class="board-expand-more" style="border-top:0;padding-top:0;">Headlines behind this count are past the retention window and can no longer be shown.</div>`
    : "";

  const items = (row.crit || []).map((i) => evidenceRow(i, { small: true })).join("");
  const more = row.criticalTruncated
    ? `${(row.crit || []).length} of ${row.critical} shown &middot; open in review for the rest`
    : `${(row.crit || []).length} of ${row.critical} shown`;

  return `
    <div class="board-row-wrap">
      <div class="board-cols board-row">
        <div style="min-width:0;">
          <div class="row" style="gap:8px;">
            <div class="${dotCls}"></div>
            <div class="board-name">${escapeHtml(row.name)}</div>
          </div>
          <div class="board-note">${escapeHtml(row.note || "")}</div>
        </div>
        <div class="board-bar">${bar}</div>
        <div class="board-num">${formatNumber(row.items)}</div>
        <div class="board-num" style="color:var(--tx2);">${formatNumber(row.scored)}</div>
        <div>${critCell}</div>
        <div class="board-mean" style="color:${color};">${hasMean ? row.mean.toFixed(1) : "—"}</div>
        <div class="board-chev" data-chev="${escapeHtml(boardKey)}-${escapeHtml(row.key)}">
          ${
            canOpen
              ? `<svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="2"
                      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m6 8 4 4 4-4"></path></svg>`
              : ""
          }
        </div>
      </div>
      <div class="board-expand hidden" id="exp-${escapeHtml(boardKey)}-${escapeHtml(row.key)}">
        <div class="board-expand-label">Critical items behind this row</div>
        ${items}
        <div class="board-expand-more">${more}</div>
        ${expiredNote}
      </div>
    </div>`;
}

export function boardCard(board) {
  const rows = (board.rows || []).map((r) => boardRow(r, board.key)).join("");
  const empty = `<div class="state-block"><div class="state-body" style="margin:0;">
      No rows in this window yet. Run a collection and this board fills in.</div></div>`;
  return `
    <div class="card">
      <div class="row-between" style="align-items:flex-start;">
        <div style="min-width:0;">
          <h3>${escapeHtml(board.title)}</h3>
          <p class="hint" style="margin-bottom:0;max-width:700px;">${escapeHtml(board.blurb)}</p>
        </div>
        ${winChip(board.window, board.windowKind)}
      </div>
      <div class="board-cols board-head" style="margin-top:18px;">
        <div class="board-th">Entity</div>
        <div class="board-th board-bar">Mean severity</div>
        <div class="board-th board-th-r">Items</div>
        <div class="board-th board-th-r">Scored</div>
        <div class="board-th board-th-r">Critical</div>
        <div class="board-th board-th-r">Mean</div>
        <div></div>
      </div>
      ${(board.rows || []).length ? rows : empty}
      ${board.verdict ? `<div class="board-verdict">${escapeHtml(board.verdict)}</div>` : ""}
    </div>`;
}

// ── tab: Risk lines ──────────────────────────────────────────────────

function riskCard(line) {
  const spark = (line.spark || []).length
    ? `<div class="risk-spark">${line.spark
        .map(
          (v) =>
            `<div class="risk-spark-bar" style="height:${Math.max(4, Math.round(v * 100))}%;background:${
              line.tone === "bad" ? "var(--red)" : line.tone === "warn" ? "var(--amber)" : "var(--ac2)"
            };opacity:.8;"></div>`
        )
        .join("")}</div>`
    : `<div class="risk-spark" style="align-items:center;"><div class="hatch"></div></div>`;

  return `
    <div class="card card-sm" style="display:flex;flex-direction:column;gap:12px;">
      <div class="row-between" style="align-items:flex-start;gap:8px;">
        <div class="kpi-label" style="margin-top:0;line-height:1.35;">${escapeHtml(line.label)}</div>
        <div class="dir-chip ${toneClass(line.tone)}">${dirIcon(line.direction)}${escapeHtml(line.directionLabel)}</div>
      </div>
      <div>
        <div class="row" style="align-items:baseline;gap:6px;">
          <div class="risk-value"${line.tone === "bad" ? ' style="color:var(--red);"' : ""}>${escapeHtml(line.value)}</div>
          <div style="font-size:11.5px;color:var(--tx3);">${escapeHtml(line.unit)}</div>
        </div>
        <div class="risk-sub">${escapeHtml(line.sub)}</div>
      </div>
      ${spark}
      <div class="risk-ev">${escapeHtml(line.evidence)}</div>
      <div style="align-self:flex-start;">${winChip(line.window, line.windowKind)}</div>
    </div>`;
}

function renderRisk(data) {
  const lines = data.lines || [];
  const p = data.provenance || {};
  return `
    <div style="display:flex;flex-direction:column;gap:12px;">
      <div class="row-between" style="align-items:flex-end;">
        <div style="min-width:0;">
          <h3 style="font-size:16px;font-weight:600;letter-spacing:-.4px;margin:0 0 4px;">Risk lines</h3>
          <p style="color:var(--tx3);font-size:12.5px;margin:0;line-height:1.5;max-width:760px;">
            The lines this desk carries on the register. Each states what moved, over the window its own evidence
            actually covers &mdash; market lines run a full twelve months, news-derived lines only as far as
            collection reached.</p>
        </div>
        ${winChip("Windows differ by line", "market")}
      </div>
      <div class="risk-strip">${lines.map(riskCard).join("")}</div>
    </div>

    ${evidenceCard(
      "What moved this week",
      "The highest-scoring items behind the lines above, each with the reason the model recorded when it scored it.",
      data.movedWindow || "Most recent monitored days",
      "news",
      data.moved || []
    )}

    <div class="card" style="background:var(--panel2);">
      <h3>What is behind these numbers</h3>
      <p class="hint">Two different windows sit on this screen. Anything taken to a board should be cited with the
        one it belongs to.</p>
      <div class="grid-3">
        <div class="standing-card">
          <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--actx);margin-bottom:9px;">
            Market &amp; macro</div>
          <div style="font-size:11.5px;color:var(--tx2);line-height:1.65;">${escapeHtml(p.market || "")}</div>
        </div>
        <div class="standing-card">
          <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--amber);margin-bottom:9px;">
            News-derived</div>
          <div style="font-size:11.5px;color:var(--tx2);line-height:1.65;">${escapeHtml(p.news || "")}</div>
        </div>
        <div class="standing-card">
          <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);margin-bottom:9px;">
            Not available yet</div>
          <div style="font-size:11.5px;color:var(--tx2);line-height:1.65;">${escapeHtml(p.unavailable || "")}</div>
        </div>
      </div>
    </div>`;
}

// ── tab: Market ──────────────────────────────────────────────────────

const SERIES_COLORS = ["var(--red)", "var(--violet)", "var(--ac2)", "var(--green)"];

function indexedChart(labels, series) {
  if (!series.length || !labels.length) return "";
  const all = series.flatMap((s) => s.values).filter((v) => Number.isFinite(v));
  if (!all.length) return "";
  const lo = Math.min(100, ...all) - 4;
  const hi = Math.max(100, ...all) + 4;
  const span = hi - lo || 1;
  const W = 640;
  const H = 190;
  const x = (i) => (labels.length === 1 ? W / 2 : 10 + (i * (W - 20)) / (labels.length - 1));
  const y = (v) => H - 20 - ((v - lo) / span) * (H - 34);
  const lines = series
    .map((s, si) => {
      const pts = s.values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
      const last = s.values.length - 1;
      return `<polyline points="${pts}" fill="none" stroke="${SERIES_COLORS[si % SERIES_COLORS.length]}"
                stroke-width="${si === 0 ? 2.4 : 2}" stroke-linecap="round" stroke-linejoin="round"></polyline>
              <circle cx="${x(last).toFixed(1)}" cy="${y(s.values[last]).toFixed(1)}" r="3.4"
                fill="${SERIES_COLORS[si % SERIES_COLORS.length]}"></circle>`;
    })
    .join("");
  const legend = series
    .map(
      (s, si) =>
        `<div class="row" style="gap:7px;"><div style="width:14px;height:2px;border-radius:1px;background:${
          SERIES_COLORS[si % SERIES_COLORS.length]
        };"></div><span class="mono" style="font-size:10px;color:var(--tx2);">${escapeHtml(s.label)}</span></div>`
    )
    .join("");
  return `
    <div class="row" style="gap:16px;margin:18px 0 10px;">
      ${legend}
      <div class="row" style="gap:7px;"><div style="width:14px;border-top:1px dashed var(--line2);"></div>
        <span class="mono" style="font-size:10px;color:var(--tx4);">BASE 100</span></div>
    </div>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
         aria-label="Indexed price performance, rebased to 100 at the start of the window"
         style="width:100%;height:200px;display:block;overflow:visible;">
      <line x1="10" y1="${y(100).toFixed(1)}" x2="${W - 10}" y2="${y(100).toFixed(1)}"
            stroke="var(--line2)" stroke-width="1" stroke-dasharray="4 4"></line>
      ${lines}
    </svg>
    <div class="mono" style="display:flex;justify-content:space-between;font-size:9px;color:var(--tx4);margin-top:6px;">
      ${labels.map((l) => `<span>${escapeHtml(l)}</span>`).join("")}
    </div>`;
}

function instrumentPanel(inst) {
  const pos = Math.max(0, Math.min(1, inst.rangePos));
  const up = inst.changePct >= 0;
  return `
    <div>
      <div class="row-between" style="margin-bottom:10px;">
        <div>
          <div class="board-name">${escapeHtml(inst.name)}</div>
          <div class="mono" style="font-size:9.5px;color:var(--tx4);margin-top:2px;">${escapeHtml(inst.code)}</div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:21px;font-weight:700;letter-spacing:-.8px;line-height:1;">&#2547;${escapeHtml(
            inst.last.toFixed(2)
          )}</div>
          <div class="mono" style="font-size:10.5px;font-weight:600;margin-top:4px;color:${
            up ? "var(--green)" : "var(--red)"
          };">${up ? "▲" : "▼"} ${Math.abs(inst.changePct).toFixed(2)}%</div>
        </div>
      </div>
      <div class="range-bar">
        <div class="range-mark" style="left:${(pos * 100).toFixed(1)}%;background:${
          pos < 0.34 ? "var(--red)" : pos > 0.66 ? "var(--green)" : "var(--amber)"
        };"></div>
      </div>
      <div class="range-axis">
        <span>${escapeHtml(inst.w52Low.toFixed(2))}</span>
        <span style="color:${inst.belowHighPct > 15 ? "var(--red)" : "var(--tx3)"};">
          ${inst.belowHighPct.toFixed(1)}% BELOW HIGH</span>
        <span>${escapeHtml(inst.w52High.toFixed(2))}</span>
      </div>
      <div class="row" style="gap:20px;margin-top:12px;">
        <div>
          <div style="font-size:12.5px;font-weight:600;">&#2547;${formatNumber(Math.round(inst.marketCapMn))}<span
            style="font-size:10px;color:var(--tx3);font-weight:400;">mn</span></div>
          <div class="watch-stat-lbl">Market cap</div>
        </div>
        ${
          inst.freeFloatMn !== null && inst.freeFloatMn !== undefined
            ? `<div><div style="font-size:12.5px;font-weight:600;">&#2547;${formatNumber(
                Math.round(inst.freeFloatMn)
              )}<span style="font-size:10px;color:var(--tx3);font-weight:400;">mn</span></div>
               <div class="watch-stat-lbl">Free float</div></div>`
            : ""
        }
      </div>
    </div>`;
}

function renderMarket(data) {
  const insts = data.instruments || [];
  if (!insts.length) {
    return `<div class="card"><div class="state-block">
      <div class="state-title">No market data stored yet</div>
      <div class="state-body">The exchange fetch has not run, or it could not reach the exchange. This panel fills in
        once it has: nothing here is estimated in the meantime.</div></div></div>`;
  }
  const months = data.months || [];
  const codes = insts.map((i) => i.code);
  const head = codes.map((c) => `<div class="board-th board-th-r">${escapeHtml(c)} close</div>
      <div class="board-th board-th-r">${escapeHtml(c)} MoM</div>`).join("");
  const cols = `110px repeat(${codes.length * 2}, minmax(0,1fr))`;
  const rows = months
    .map((m) => {
      const cells = codes
        .map((c) => {
          const close = m.closes[c];
          const mom = m.moms[c];
          const momTxt =
            mom === null || mom === undefined
              ? `<span style="color:var(--tx4);">—</span>`
              : `<span style="color:${mom >= 0 ? "var(--green)" : "var(--red)"};">${
                  mom >= 0 ? "+" : "−"
                }${Math.abs(mom).toFixed(1)}%</span>`;
          return `<div class="mono" style="text-align:right;font-size:12.5px;font-weight:600;">${
            close === null || close === undefined ? "—" : close.toFixed(2)
          }</div>
          <div class="mono" style="text-align:right;font-size:11.5px;font-weight:600;">${momTxt}</div>`;
        })
        .join("");
      return `<div style="display:grid;grid-template-columns:${cols};gap:14px;align-items:center;padding:9px 0;
                border-bottom:1px solid var(--line);">
        <div class="mono" style="font-size:11px;color:var(--tx2);">${escapeHtml(m.label)}</div>${cells}</div>`;
    })
    .join("");

  return `
    <div class="grid-2-long">
      <div class="card" style="display:flex;flex-direction:column;">
        <div class="row-between" style="align-items:flex-start;">
          <div style="min-width:0;">
            <h3>Market position</h3>
            <p class="hint" style="margin-bottom:0;max-width:520px;">${escapeHtml(data.note || "")}</p>
          </div>
          ${winChip(`${data.tradingDays} trading days`, "market")}
        </div>
        ${indexedChart(data.labels || [], data.series || [])}
      </div>
      <div class="card">
        <h3>Where each stock sits</h3>
        <p class="hint">Position inside the 52-week range says more about pressure than a day&rsquo;s move does.</p>
        <div style="display:flex;flex-direction:column;gap:18px;">
          ${insts.map(instrumentPanel).join('<div class="section-divider"></div>')}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="row-between" style="align-items:flex-start;">
        <div style="min-width:0;">
          <h3>Month-end closes</h3>
          <p class="hint" style="margin-bottom:0;max-width:700px;">The last traded close in each month, and the move
            from the month before. The chart above is for reading; this is for citing.</p>
        </div>
        ${winChip(data.window || "", "market")}
      </div>
      <div style="display:grid;grid-template-columns:${cols};gap:14px;padding:14px 0 9px;
                  border-bottom:1px solid var(--line);margin-top:18px;">
        <div class="board-th">Month</div>${head}
      </div>
      ${
        rows ||
        `<div class="state-block"><div class="state-body" style="margin:0;">No month in this window has a stored close.</div></div>`
      }
      <div class="board-verdict">Source: DSE day-end archive, as of ${escapeHtml(data.asOf || "")}.</div>
    </div>`;
}

// ── tab: Economy ─────────────────────────────────────────────────────

function macroCard(card) {
  const statusColor =
    card.status === "live" ? "var(--green)" : card.status === "annual" ? "var(--amber)" : "var(--tx4)";
  const body = card.hasSeries && (card.series || []).length
    ? `<div class="risk-spark" style="height:22px;margin-top:14px;">${card.series
        .map((v) => `<div class="risk-spark-bar" style="height:${Math.max(4, Math.round(v * 100))}%;background:var(--ac2);opacity:.75;"></div>`)
        .join("")}</div>`
    : `<div style="height:22px;display:flex;align-items:center;margin-top:14px;"><div class="hatch"></div></div>`;
  return `
    <div class="card card-sm">
      <div class="row-between" style="margin-bottom:12px;">
        <div class="kpi-label" style="margin-top:0;">${escapeHtml(card.label)}</div>
        <div class="mono" style="font-size:9px;color:${statusColor};text-transform:uppercase;">${escapeHtml(
          card.status
        )}</div>
      </div>
      <div class="row" style="align-items:baseline;gap:6px;">
        <div class="trend-card-value">${escapeHtml(card.value)}</div>
        <div class="trend-card-unit">${escapeHtml(card.unit)}</div>
      </div>
      ${body}
      <div style="font-size:11px;color:var(--tx3);margin-top:11px;line-height:1.5;">${escapeHtml(card.note || "")}</div>
    </div>`;
}

function renderEconomy(data) {
  const macro = data.macro || [];
  return `
    <div class="grid-3">${macro.map(macroCard).join("")}</div>
    ${data.board ? boardCard(data.board) : ""}`;
}

// ── tab: Telecom watch ───────────────────────────────────────────────

function watchlistCard(wl) {
  const groups = wl.groups || [];
  const entities = wl.entities || [];
  const cards = groups
    .map((g) => {
      const mine = entities.filter((e) => e.groupKey === g.key);
      const chips = mine
        .map((e) => {
          const cls = e.custom ? "wl-chip wl-chip-custom" : e.key === "grameenphone" ? "wl-chip wl-chip-us" : "wl-chip";
          const del = e.custom
            ? ` <button type="button" class="btn-ghost js-wl-del" data-id="${escapeHtml(String(e.id))}"
                 aria-label="Remove ${escapeHtml(e.label)}" style="font-size:11px;color:var(--red);padding:0 0 0 4px;">&times;</button>`
            : "";
          return `<span class="${cls}">${escapeHtml(e.label)}${del}</span>`;
        })
        .join("");
      return `<div class="wl-group">
          <div class="row-between" style="margin-bottom:10px;">
            <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);">${escapeHtml(
              g.title
            )}</div>
            <div class="mono" style="font-size:11px;font-weight:600;color:var(--tx2);">${mine.length}</div>
          </div>
          <div class="wl-chips">${chips}</div>
        </div>`;
    })
    .join("");

  return `
    <div class="card">
      <div class="row-between" style="align-items:flex-start;">
        <div style="min-width:0;">
          <h3>Watchlist</h3>
          <p class="hint" style="margin-bottom:0;max-width:700px;">${entities.length} entities across
            ${groups.length} boards. Anyone can add one, but no entity goes live until its terms have been run against
            the archive and the matches look right.</p>
        </div>
        <button type="button" id="wlAddBtn" class="btn btn-soft" style="display:flex;align-items:center;gap:7px;">
          <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.9"
               stroke-linecap="round" aria-hidden="true"><path d="M10 4.5v11M4.5 10h11"></path></svg>
          Add an entity
        </button>
      </div>
      <div class="wl-groups" style="margin-top:20px;">${cards}</div>
      <div id="wlAddPanel" class="wl-add hidden">
        <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--actx);margin-bottom:14px;">
          Add an entity</div>
        <div class="wl-add-grid">
          <div style="display:flex;flex-direction:column;gap:14px;">
            <div>
              <label class="field-label" for="wlLabel">Name</label>
              <input id="wlLabel" class="text-input" type="text" autocomplete="off" placeholder="Nagad">
            </div>
            <div>
              <label class="field-label" for="wlEn">English terms (comma separated)</label>
              <input id="wlEn" class="text-input" type="text" autocomplete="off" placeholder="nagad">
            </div>
            <div>
              <label class="field-label" for="wlBn">Bangla terms (comma separated)</label>
              <input id="wlBn" class="text-input" type="text" autocomplete="off" placeholder="&#2472;&#2455;&#2470;">
            </div>
            <div>
              <label class="field-label" for="wlGroup">Board</label>
              <select id="wlGroup" class="text-input">
                ${groups.map((g) => `<option value="${escapeHtml(g.key)}">${escapeHtml(g.title)}</option>`).join("")}
              </select>
            </div>
            <div>
              <label class="field-label" for="wlNote">Note (optional)</label>
              <input id="wlNote" class="text-input" type="text" autocomplete="off">
            </div>
            <div class="row" style="gap:9px;">
              <button type="button" id="wlPreviewBtn" class="btn btn-outline">Check these terms</button>
              <button type="button" id="wlCancelBtn" class="btn btn-ghost">Cancel</button>
            </div>
          </div>
          <div id="wlPreview">
            <div class="card" style="background:var(--panel);padding:16px 18px;">
              <div style="font-size:11.5px;color:var(--tx3);line-height:1.6;">
                A Bangla term is checked against the archive before it can be saved, because Bangla word boundaries do
                not work the way Latin ones do. &#2480;&#2476;&#2495; matched 222 articles before anyone looked; two
                were the operator and the rest were Sunday and a common given name.
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>`;
}

function renderTelecom(data) {
  return `
    ${data.peers ? boardCard(data.peers) : ""}
    ${data.wallets && (data.wallets.rows || []).length ? boardCard(data.wallets) : ""}
    ${evidenceCard(
      "What the operators are doing",
      "Commercial and network moves by the operator set, highest-scoring first. Read alongside the register below: a peer's launch and a peer's fine are both forecasts of our own, in opposite directions.",
      "Scored items, all sources",
      "news",
      data.initiatives || []
    )}
    ${data.regulatory ? boardCard(data.regulatory) : ""}
    ${data.watchlist ? watchlistCard(data.watchlist) : ""}`;
}

// ── public render + bind ─────────────────────────────────────────────

export function longTabsHtml(active) {
  return `<div class="long-tabs" role="tablist">${LONG_TAB_LABELS.map(
    ([key, label]) =>
      `<button type="button" role="tab" aria-selected="${key === active}" class="long-tab${
        key === active ? " active" : ""
      }" data-longtab="${key}">${label}</button>`
  ).join("")}</div>`;
}

export function renderLongTab(tab, data) {
  if (tab === "market") return renderMarket(data || {});
  if (tab === "economy") return renderEconomy(data || {});
  if (tab === "telecom") return renderTelecom(data || {});
  return renderRisk(data || {});
}

/**
 * Wire the interactive bits of whichever tab was just rendered.
 * `reload` re-fetches the current tab (used after the watchlist changes,
 * because adding an entity changes every board on the screen).
 */
export function bindLongTab(root, tab, { navigate, reload }) {
  root.querySelectorAll("[data-longtab]").forEach((btn) => {
    btn.addEventListener("click", () => navigate("dash", { dashMode: "long", longTab: btn.dataset.longtab }));
  });

  root.querySelectorAll(".js-crit").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = `exp-${btn.dataset.board}-${btn.dataset.key}`;
      const panel = root.querySelector(`#${CSS.escape(id)}`);
      const chev = root.querySelector(`[data-chev="${btn.dataset.board}-${btn.dataset.key}"]`);
      if (!panel) return;
      const open = panel.classList.toggle("hidden") === false;
      btn.setAttribute("aria-expanded", String(open));
      if (chev) chev.classList.toggle("open", open);
    });
  });

  root.querySelectorAll(".js-open-review").forEach((btn) => {
    btn.addEventListener("click", () => navigate("review", { focusArticle: Number(btn.dataset.article) }));
  });

  if (tab === "telecom") bindWatchlist(root, reload);
}

function bindWatchlist(root, reload) {
  const addBtn = root.querySelector("#wlAddBtn");
  const panel = root.querySelector("#wlAddPanel");
  const cancel = root.querySelector("#wlCancelBtn");
  const preview = root.querySelector("#wlPreviewBtn");
  const out = root.querySelector("#wlPreview");
  if (!addBtn || !panel) return;

  addBtn.addEventListener("click", () => {
    panel.classList.toggle("hidden");
    if (!panel.classList.contains("hidden")) root.querySelector("#wlLabel")?.focus();
  });
  cancel?.addEventListener("click", () => panel.classList.add("hidden"));

  const terms = (sel) =>
    String(root.querySelector(sel)?.value || "")
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);

  preview?.addEventListener("click", async () => {
    const enTerms = terms("#wlEn");
    const bnTerms = terms("#wlBn");
    if (!enTerms.length && !bnTerms.length) {
      out.innerHTML = `<div class="banner banner-warn">Enter at least one term before checking.</div>`;
      return;
    }
    out.innerHTML = `<div class="card" style="background:var(--panel);padding:16px 18px;">${
      `<div class="skel skel-row"></div>`.repeat(3)
    }</div>`;
    try {
      const res = await api.post("/watchlist/preview", {
        enTerms,
        bnTerms,
        groupKey: root.querySelector("#wlGroup")?.value,
      });
      out.innerHTML = previewHtml(res);
      out.querySelector("#wlSaveGuard")?.addEventListener("click", () => save(res.guard?.key ?? null));
      out.querySelector("#wlSavePlain")?.addEventListener("click", () => save(null));
    } catch (err) {
      out.innerHTML = `<div class="banner banner-error">${escapeHtml(err.message || "Could not check those terms.")}</div>`;
    }
  });

  // `guardKey` is the guard the preview actually ran (or null for
  // "add it unguarded"). It is never invented here — the server picked
  // it from the board and named it in the preview response, so what gets
  // saved is exactly what the operator was shown.
  async function save(guardKey) {
    try {
      await api.post("/watchlist", {
        groupKey: root.querySelector("#wlGroup")?.value,
        label: root.querySelector("#wlLabel")?.value || "",
        note: root.querySelector("#wlNote")?.value || "",
        enTerms: terms("#wlEn"),
        bnTerms: terms("#wlBn"),
        context: guardKey,
      });
      reload();
    } catch (err) {
      out.innerHTML = `<div class="banner banner-error">${escapeHtml(err.message || "Could not save that entity.")}</div>`;
    }
  }
}

function previewHtml(res) {
  const sample = (rows, tone) =>
    rows
      .map(
        (r) => `<div class="wl-sample">
          ${
            tone === "bad"
              ? `<svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="var(--red)" stroke-width="2.2"
                   stroke-linecap="round" style="flex:0 0 12px;margin-top:2px;" aria-hidden="true">
                   <path d="M5 5l10 10M15 5 5 15"></path></svg>`
              : `<svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="var(--green)" stroke-width="2.2"
                   stroke-linecap="round" stroke-linejoin="round" style="flex:0 0 12px;margin-top:2px;" aria-hidden="true">
                   <path d="M4 10.5 8 14.5 16 6"></path></svg>`
          }
          <div class="wl-sample-t">${escapeHtml(r.title)}</div></div>`
      )
      .join("");

  const guarded =
    res.guardedTotal === null || res.guardedTotal === undefined || !res.guard
      ? ""
      : `<div style="padding-top:14px;border-top:1px solid var(--line);margin-top:14px;">
           <div style="font-size:13px;font-weight:600;margin-bottom:5px;">With ${escapeHtml(
             res.guard.label.toLowerCase()
           )} required &mdash; ${res.guardedTotal} matches.</div>
           <div style="font-size:11.5px;color:var(--tx3);line-height:1.6;">${escapeHtml(res.guard.blurb)}</div>
           ${
             res.guardedSample?.length
               ? `<div style="display:flex;flex-direction:column;gap:8px;margin-top:12px;">${sample(
                   res.guardedSample,
                   "ok"
                 )}</div>`
               : ""
           }
         </div>`;

  return `
    <div class="card" style="background:var(--panel);padding:16px 18px;">
      <div style="font-size:13px;font-weight:600;margin-bottom:8px;">${escapeHtml(res.message || "")}</div>
      ${
        res.unguardedSample?.length
          ? `<div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx4);margin:12px 0 8px;">
               What the terms catch on their own</div>
             <div style="display:flex;flex-direction:column;gap:8px;">${sample(
               res.unguardedSample,
               res.suggestGuard ? "bad" : "ok"
             )}</div>`
          : ""
      }
      ${guarded}
      <div class="row" style="gap:9px;margin-top:15px;flex-wrap:wrap;">
        ${
          res.guard && res.guardedTotal !== null && res.guardedTotal !== undefined
            ? `<button type="button" id="wlSaveGuard" class="btn ${
                res.suggestGuard ? "btn-primary" : "btn-outline"
              }">Add with this guard</button>`
            : ""
        }
        <button type="button" id="wlSavePlain" class="btn ${res.suggestGuard ? "btn-outline" : "btn-primary"}">
          ${res.suggestGuard ? "Add unguarded anyway" : "Add as it is"}</button>
      </div>
    </div>`;
}

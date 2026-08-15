// Small shared helpers used across screens. Deliberately dependency-free
// (no framework, per the mission's no-npm constraint) — except for
// ensureRiskSections() below, which needs api.js to populate
// RISK_SECTIONS for real; every other export here stays dependency-free.

import { api } from "./api.js";

export function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Fixed score-tier taxonomy (frontend-contract.md §3.1/§3.3, from the
// design's own `tier(score)`). Flagged as an open question in §8
// (whether band thresholds are ever admin-configurable) — hardcoded
// here as the prototype does, since no admin surface for it exists yet.
export function tier(score) {
  const n = Number(score);
  if (n >= 8) return { key: "red", label: "High", color: "var(--red)", bg: "var(--redsoft)" };
  if (n >= 6) return { key: "yellow", label: "Medium", color: "var(--amber)", bg: "var(--ambersoft)" };
  if (n >= 5) return { key: "green", label: "Low", color: "var(--green)", bg: "var(--greensoft)" };
  return { key: "magenta", label: "Least", color: "var(--violet)", bg: "var(--violetsoft)" };
}

// HARDCODED-SECTIONS BUG FIX: this used to be a static 5-value array.
// KEYWORDS-CONFIG made keyword sections admin-creatable (an admin can
// add e.g. a "Competitors" section with no code change), so a static
// list here meant a new section would never appear in the dashboard's
// by-section chart, export's section filter, or the prompts screen's
// section picker — every consumer of this export. Fixed by keeping
// RISK_SECTIONS as a plain, mutable `let` binding (still the exact same
// array-of-strings shape every importer already expects) and populating
// it for real via ensureRiskSections() below, instead of turning it
// into a function — export.js/prompts.js import and use it as a plain
// array and are NOT touched by this task, so the shape has to stay
// exactly what it always was. ES module imports are live bindings, so a
// reassignment here (`RISK_SECTIONS = [...]`) is visible to every
// importer's next read of it, without them needing to re-import
// anything.
export let RISK_SECTIONS = ["Geopolitical", "Macroeconomy", "Telco/regulatory", "Political", "Others"];

let _riskSectionsPromise = null;

// Populates RISK_SECTIONS from the real, currently-configured section
// list, once per page load, and caches the in-flight/settled promise so
// repeated calls (every screen that renders section data can safely
// call this) don't re-fetch. Reads GET /dashboard/today's bySection[]
// rather than the admin-only GET /keywords/sections, specifically so
// this works for every authenticated role (analyst or admin) — the
// long-term dashboard tab that's the main consumer of RISK_SECTIONS is
// not an admin-only screen — and stays derived from the exact same
// app.api.schemas.core.get_risk_sections() the backend now uses, so
// frontend and backend never disagree about what "a section" is.
// Failure (not yet authenticated, network down) is a silent no-op that
// leaves the fixed 5-value fallback above in place — same pattern as
// shell.js's refreshShellCounts.
export function ensureRiskSections() {
  if (!_riskSectionsPromise) {
    _riskSectionsPromise = api
      .get("/dashboard/today")
      .then((data) => {
        const labels = ((data && data.bySection) || []).map((s) => s.label).filter(Boolean);
        if (labels.length) RISK_SECTIONS = labels;
        return RISK_SECTIONS;
      })
      .catch(() => RISK_SECTIONS);
  }
  return _riskSectionsPromise;
}

const SECTION_COLOR_PALETTE = [
  "var(--sec-geopolitical)",
  "var(--sec-macroeconomy)",
  "var(--sec-telco)",
  "var(--sec-political)",
  "var(--sec-others)",
];

export const SECTION_COLORS = {
  Geopolitical: "var(--sec-geopolitical)",
  Macroeconomy: "var(--sec-macroeconomy)",
  "Telco/regulatory": "var(--sec-telco)",
  Political: "var(--sec-political)",
  Others: "var(--sec-others)",
};

// A section RISK_SECTIONS picked up at runtime (e.g. an admin-created
// "Competitors" section) has no fixed entry in SECTION_COLORS above —
// falls back to a stable color assigned by name so it's still readable
// (not `undefined` background) and doesn't shift on every re-render.
export function sectionColor(section) {
  if (SECTION_COLORS[section]) return SECTION_COLORS[section];
  let hash = 0;
  for (let i = 0; i < section.length; i++) hash = (hash * 31 + section.charCodeAt(i)) >>> 0;
  return SECTION_COLOR_PALETTE[hash % SECTION_COLOR_PALETTE.length];
}

export function formatNumber(n) {
  const v = Number(n);
  if (Number.isNaN(v)) return String(n ?? "");
  return v.toLocaleString("en-US");
}

// Real maxima, computed from the actual data set — NOT the prototype's
// hardcoded-denominator bugs (frontend-contract.md §8 item 4:
// `standing[].width` / 214 and `candidates[].barWidth` / 12).
/** Clamp to a legal CSS bar width. Both helpers below feed
 * `style="width:..."` directly, so a value outside 0-100 does not
 * produce a wrong number — it produces a broken layout, with the bar
 * escaping its track.
 *
 * Not currently reachable: every call site passes a value drawn FROM
 * the same list/total, so the ratio cannot exceed 1. This is a guard
 * against the next caller, not a fix for a live bug — the cost is one
 * comparison and the failure mode it prevents is visual corruption
 * nobody would think to write a test for.
 */
function clampPct(n) {
  if (!Number.isFinite(n)) return 0;
  return Math.min(100, Math.max(0, n));
}

export function pctOfMax(value, list, accessor = (x) => x) {
  const max = (list || []).reduce((m, item) => Math.max(m, Number(accessor(item)) || 0), 0);
  if (max <= 0) return "0%";
  return clampPct(Math.round((Number(value) / max) * 100)) + "%";
}

export function pctOfTotal(value, total) {
  if (!total) return "0%";
  return clampPct(Math.round((Number(value) / Number(total)) * 100)) + "%";
}

export function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

export function qs(root, sel) {
  return root.querySelector(sel);
}
export function qsa(root, sel) {
  return Array.from(root.querySelectorAll(sel));
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

// SVG polyline builder for the long-term trend cards — same shape as
// the prototype's `poly()` helper (fixed 240x56 viewBox), computed
// client-side from raw monthly values per frontend-contract.md §3.2.
export function poly(vals, w, h, close) {
  if (!vals || !vals.length) return close ? `0,${h} ${w},${h}` : "";
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const pts = vals.map((v, i) => {
    const x = (i / (vals.length - 1)) * w;
    const y = h - 4 - ((v - min) / span) * (h - 10);
    return x.toFixed(1) + "," + y.toFixed(1);
  });
  if (close) return "0," + h + " " + pts.join(" ") + " " + w + "," + h;
  return pts.join(" ");
}

export function skeletonRows(n, height = 14) {
  return Array.from({ length: n })
    .map(() => `<div class="skel skel-row" style="height:${height}px;width:${60 + Math.round(Math.random() * 35)}%"></div>`)
    .join("");
}

export function emptyState(title, body, actionHtml = "") {
  return `<div class="state-block"><div class="state-title">${escapeHtml(title)}</div><p class="state-body">${escapeHtml(body)}</p>${actionHtml}</div>`;
}

export function errorBanner(message, { retryAction } = {}) {
  return `<div class="banner banner-error"><span>&#10005;</span><div style="flex:1">${escapeHtml(message)}</div>${
    retryAction ? `<button class="btn-ghost" data-action="${retryAction}" style="color:var(--red);text-decoration:underline;">Retry</button>` : ""
  }</div>`;
}

// Renders a friendly message for an endpoint that does not exist yet on
// the (mostly-stub) backend — see the mission brief: "code against
// frontend-contract.md §9's proposed shape and REPORT the gap" rather
// than inventing data. A 404 from a stub router with no routes
// registered is exactly this case, distinct from a genuine "not found".
export function notWiredBanner(endpointLabel) {
  return `<div class="banner banner-info"><span>&#9432;</span><div style="flex:1">This screen calls <code class="mono">${escapeHtml(
    endpointLabel
  )}</code>, which isn't implemented on the API yet. Once ADMIN-API/FE-WIRE ship it, this screen will populate automatically, no frontend change needed.</div></div>`;
}

export function fmtErr(err) {
  if (!err) return "Something went wrong.";
  if (err.kind === "network") return err.message;
  if (err.kind === "unauthorized") return "Your session has expired. Please sign in again.";
  if (err.kind === "forbidden") return "You don't have permission to view this.";
  if (err.kind === "not_found") return "not_implemented";
  return err.message || "Something went wrong.";
}

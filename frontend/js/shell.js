// App shell: sidebar (rail) + topbar, present on every authenticated
// screen (frontend-contract.md §2). Mounts the active screen module
// into #screenRoot via router.js.
//
// Important structural note: the shell subscribes to EVERY global state
// change (theme, rail collapse, pendingCount ticking in, progress
// pills, etc.), but the currently-mounted screen must survive all of
// that — a screen's own async data fetch resolves well after mount and
// writes into the DOM node it was given. So `#screenRoot` is created
// exactly once and its reference is reused for the app's lifetime;
// `renderChrome()` (called on every state change) only ever touches the
// sidebar/topbar containers, which are siblings of `#screenRoot`, never
// its parent — an earlier version of this file rebuilt the whole shell
// (including `#screenRoot`) via `shellRoot.innerHTML = ...` on every
// state change, which silently orphaned the mounted screen's DOM the
// moment anything else in global state changed (caught via manual QA
// against fixture data, not by a live backend — see this task's report).

import { state, setState, subscribe, NAV_STRUCTURE, SCREEN_TITLES } from "./state.js";
import { getTheme, applyTheme, themeGlyph } from "./theme.js";
import { logout } from "./auth.js";
import { escapeHtml } from "./util.js";
import { mountScreen } from "./router.js";
import { api } from "./api.js";
import { mountAccountPanel } from "./components/accountPanel.js";
import { DEFAULT_LONG_TAB, DEFAULT_SCREEN, canAccess, pathForScreen, pushRoute, readLocation } from "./route.js";

let shellRoot = null;
let railEl = null;
let topbarEl = null;
let screenRootEl = null;
let accountModalEl = null;
let currentMount = null;
let currentScreenKey = null;
let currentDashMode = null;
let accountMount = null;
let accountOpenState = false;

export function mountShell(root, { onSignOut }) {
  shellRoot = root;
  shellRoot.innerHTML = `
    <a href="#screenRoot" class="skip-link">Skip to content</a>
    <div class="app-shell">
      <!-- Landmarks, so a screen reader can jump straight to the nav or
           the screen instead of walking the whole rail every time. The
           rail is the site navigation; screenRoot is where the mounted
           screen renders and is the skip link's target. -->
      <aside id="railEl" class="rail" aria-label="Main navigation"></aside>
      <main class="main-area">
        <div id="topbarEl" class="topbar"></div>
        <div id="screenRoot" tabindex="-1"></div>
      </main>
    </div>
    <div id="accountModalEl"></div>
  `;
  railEl = shellRoot.querySelector("#railEl");
  topbarEl = shellRoot.querySelector("#topbarEl");
  screenRootEl = shellRoot.querySelector("#screenRoot");
  accountModalEl = shellRoot.querySelector("#accountModalEl");

  renderChrome();
  syncScreen();
  syncAccountPanel();
  subscribe(() => {
    renderChrome();
    syncScreen();
    syncAccountPanel();
  });
  refreshShellCounts();
  startRunStatusPolling();
  return { navigate };
}

// BUGFIX-NAV: the header's live-status pill used to only ever reflect
// dashboard.js's OWN in-memory pipeline object — real status while that
// screen happened to be mounted, nothing at all once an admin navigated
// away (the whole point of this mission's fix: a run started from the
// Dashboard used to look "finished" the instant you left it, even
// though fetch/match/enrich might still genuinely be running server-
// side). Polled here instead, at the app-shell level — mounted once for
// the app's lifetime, independent of which screen is currently active —
// against the same consolidated GET /health/pipeline/run/status
// dashboard.js's own poll uses (app.services.admin.run_status). Purely
// observational, same as that poll: this never starts or steers a run,
// only renders whatever the server-side background thread is actually
// doing right now.
let runPollTimer = null;
let runPollInFlight = false;

function startRunStatusPolling() {
  if (runPollTimer) return;
  runPollTimer = setInterval(pollRunStatus, 2000);
  pollRunStatus();
}

// THE ONLY poller for run status in the whole app.
//
// The Run screen used to open a SECOND interval against this same
// endpoint. Both ran whenever that screen was mounted: 30 req/min here
// plus 40 req/min there = 70 req/min of pure polling, against a
// whole-API limit of 120 requests / 60 seconds per IP
// (`Settings.rate_limit_requests_per_window`, enforced by
// RateLimitMiddleware). Ordinary navigation on top of that crossed the
// limit and the API started returning 429 — surfaced in the UI as
// "Too many requests. Slow down and try again shortly." while a run was
// perfectly healthy underneath.
//
// Fixed by publishing the result to `state.runStatus` and having the
// Run screen SUBSCRIBE. One request per interval regardless of how many
// views are showing run progress, and every view sees the same snapshot
// at the same time.
export async function pollRunStatus() {
  // Gated to admins (same as the "Run collection" control itself) so a
  // non-admin session — or the pre-login screen, where state.user is
  // still null — never bothers an admin-only route only to eat a
  // guaranteed 403 every 2 seconds.
  if (!(state.user && state.user.is_admin) || runPollInFlight) return;
  runPollInFlight = true;
  try {
    const data = await api.get("/health/pipeline/run/status");
    applyRunStatusToProgress(data);
  } catch (err) {
    // Silent — shell chrome, not worth a banner for a transient poll
    // failure (session edge, brief network blip). Deliberately does NOT
    // clear `state.runStatus`: a dropped poll must not make a running
    // job look finished.
  } finally {
    runPollInFlight = false;
  }
}

function applyRunStatusToProgress(data) {
  const text = runStatusProgressText(data);
  const patch = {};
  if (state.progress !== text) patch.progress = text;
  // Always republish the snapshot — subscribers re-render from it, and
  // its nested fields change even when the header text does not.
  patch.runStatus = data;
  setState(patch);
}

function runStatusProgressText(data) {
  if (!data || data.status !== "running") return "";
  if (data.phase === "fetch" && data.fetch) {
    return `Run collection · fetching (${data.fetch.completed || 0}/${data.fetch.total_sources || "?"} sources)`;
  }
  if (data.phase === "match") return "Run collection · matching keywords";
  if (data.phase === "enrich" && data.enrich) {
    const stages = data.enrich.stages || [];
    const doneCount = stages.filter((s) => s.status === "done" || s.status === "error").length;
    const current = stages.find((s) => s.status === "running");
    return `Run collection · ${current ? current.name : "enrich"} (${doneCount}/${stages.length})`;
  }
  return "Run collection · starting…";
}

function syncAccountPanel() {
  if (state.accountPanelOpen === accountOpenState) return;
  accountOpenState = state.accountPanelOpen;
  if (accountOpenState) {
    accountMount = mountAccountPanel(accountModalEl, { onClose: () => setState({ accountPanelOpen: false }) });
  } else {
    if (accountMount && accountMount.destroy) accountMount.destroy();
    accountMount = null;
    accountModalEl.innerHTML = "";
  }
}

/**
 * Change screen AND put it in the address bar.
 *
 * The URL is written here rather than in a state subscriber so that a
 * navigation is one atomic action: screen, mode and address always
 * agree. `popstate` (Back/Forward) takes the opposite path — it updates
 * state WITHOUT pushing, since the browser has already moved history.
 */
export function navigate(screenKey, opts = {}) {
  const target = canAccess(screenKey, state.user) ? screenKey : DEFAULT_SCREEN;
  const next = { screen: target, ...opts };
  setState(next);
  pushRoute(
    target,
    { dashMode: next.dashMode ?? state.dashMode, longTab: next.longTab ?? state.longTab },
    { replace: target !== screenKey }
  );
}

/**
 * Adopt whatever the URL currently says. Called once after login (so a
 * deep link survives the auth round-trip) and on every Back/Forward.
 *
 * An unroutable path, or an admin screen requested by a non-admin,
 * REPLACES the URL rather than pushing — otherwise Back would walk
 * straight back into the URL we just rejected.
 */
export function applyRouteFromUrl() {
  const { screen, dashMode, longTab } = readLocation();
  const allowed = canAccess(screen, state.user) ? screen : DEFAULT_SCREEN;
  setState({ screen: allowed, dashMode, longTab });
  if (allowed !== screen || pathForScreen(allowed, { dashMode, longTab }) !== window.location.pathname) {
    pushRoute(allowed, { dashMode, longTab }, { replace: true });
  }
}

let popstateBound = false;

/** Wire Back/Forward once per app lifetime. */
export function bindHistoryNavigation() {
  if (popstateBound || typeof window === "undefined") return;
  popstateBound = true;
  window.addEventListener("popstate", () => {
    // State only — the browser already moved the history pointer, so
    // pushing here would fight it and make Back require two presses.
    const { screen, dashMode, longTab } = readLocation();
    const allowed = canAccess(screen, state.user) ? screen : DEFAULT_SCREEN;
    setState({ screen: allowed, dashMode, longTab });
  });
}

// The Long-term kicker names the sub-tab, because the four tabs carry
// different windows — market runs twelve real months, the news-derived
// boards only as far as collection reached — and the header is where a
// reader looks to know which one they are reading.
const LONG_KICKERS = {
  risk: "Long term · risk lines",
  market: "Long term · market · twelve months",
  economy: "Long term · economy and companies",
  telecom: "Long term · telecom watch",
};

function longKicker() {
  return LONG_KICKERS[state.longTab] || LONG_KICKERS[DEFAULT_LONG_TAB];
}

function pageTitleFor() {
  if (state.screen === "dash") {
    return [
      "Dashboard",
      state.dashMode === "short" ? "Short term · today and this fortnight" : longKicker(),
    ];
  }
  return SCREEN_TITLES[state.screen] || ["", ""];
}

// Mounts/unmounts the active screen into the stable #screenRoot node.
// Only runs the (potentially expensive, data-fetching) mount when the
// screen or dashboard tab actually changed — every other state change
// (theme, rail, counts ticking in) leaves the mounted screen alone.
function syncScreen() {
  // The Long-term sub-tab is deliberately NOT part of this comparison.
  // Switching sub-tabs must not tear down and remount the whole screen —
  // the dashboard mount owns its own tab switching and fetches only the
  // tab that was asked for, which is the entire reason the tabs were
  // split into separate endpoints.
  if (currentMount && currentScreenKey === state.screen && currentDashMode === state.dashMode) {
    return;
  }
  if (currentMount && currentMount.destroy) currentMount.destroy();
  currentMount = mountScreen(state.screen, screenRootEl, { navigate });
  currentScreenKey = state.screen;
  currentDashMode = state.dashMode;
}

function renderChrome() {
  if (!shellRoot) return;
  const isAdmin = !!(state.user && state.user.is_admin);
  const [title, kicker] = pageTitleFor();

  railEl.className = "rail" + (state.railOpen ? "" : " collapsed");
  railEl.innerHTML = `
    <div class="rail-head">
      <div class="rail-logo">ERM</div>
      ${
        state.railOpen
          ? `<div style="min-width:0;flex:1;">
              <div style="font-size:13px;font-weight:600;letter-spacing:-.2px;white-space:nowrap;">ERM Desk</div>
              <div class="mono" style="font-size:9px;letter-spacing:1.1px;text-transform:uppercase;color:var(--tx4);margin-top:1px;">${
                isAdmin ? "Administrator" : "Analyst"
              }</div>
            </div>`
          : ""
      }
    </div>
    <div class="rail-body">${renderNavGroups(isAdmin)}</div>
    <div class="rail-foot">
      <div class="rail-user">
        <div class="rail-avatar">${escapeHtml((state.user && state.user.initials) || "?")}</div>
        ${
          state.railOpen
            ? `<div style="min-width:0;flex:1;">
                <div class="rail-user-name">${escapeHtml((state.user && state.user.display_name) || "")}</div>
                <div class="rail-user-email">${escapeHtml((state.user && state.user.email) || "")}</div>
              </div>
              <button id="accountBtn" class="rail-signout" title="Account and security" aria-label="Account and security">&#9881;</button>
              <button id="signOutBtn" class="rail-signout" title="Sign out" aria-label="Sign out">&#9211;</button>`
            : ""
        }
      </div>
      <button id="railToggleBtn" class="rail-toggle" aria-expanded="${state.railOpen ? "true" : "false"}" aria-label="${state.railOpen ? "Collapse the navigation rail" : "Expand the navigation rail"}">${state.railOpen ? "‹ Collapse" : "›"}</button>
    </div>
  `;

  topbarEl.innerHTML = `
    <div style="min-width:0;">
      <div class="topbar-title">${escapeHtml(title)}</div>
      <div class="topbar-kicker mono">${escapeHtml(kicker || "")}</div>
    </div>
    <div class="grow"></div>
    ${
      state.progress
        ? `<div class="progress-pill">
            <span class="progress-dot"></span>
            <span style="font-size:12px;color:var(--actx);white-space:nowrap;">${escapeHtml(state.progress)}</span>
          </div>`
        : ""
    }
    <span class="today-label">${escapeHtml(todayLabel())}</span>
    <button id="topThemeBtn" class="icon-btn" title="Switch theme" aria-label="Switch between light and dark theme">${themeGlyph(getTheme())}</button>
    <button id="goReviewBtn" class="btn btn-primary" style="display:flex;align-items:center;gap:9px;padding:9px 15px;white-space:nowrap;">
      Review queue
      ${
        state.pendingCount !== undefined && state.pendingCount !== null
          ? `<span class="mono" style="font-size:10.5px;padding:1px 6px;border-radius:999px;background:rgba(3,32,42,.2);">${state.pendingCount}</span>`
          : ""
      }
    </button>
  `;

  bind();
}

function renderNavGroups(isAdmin) {
  return NAV_STRUCTURE.filter((g) => !g.adminOnly || isAdmin)
    .map(
      (grp) => `
      <div class="rail-group">
        ${state.railOpen ? `<div class="rail-group-label">${escapeHtml(grp.label)}</div>` : ""}
        ${grp.items.map((it) => renderNavItem(it)).join("")}
      </div>
    `
    )
    .join("");
}

function renderNavItem(it) {
  const active = state.screen === it.key;
  const badgeVal = it.badgeKey ? state[it.badgeKey] : undefined;
  const hasBadge = badgeVal !== undefined && badgeVal !== null && badgeVal !== "";
  return `
    <button class="rail-item ${active ? "active" : ""}" data-nav="${it.key}" title="${escapeHtml(it.label)}">
      <span class="rail-icon">${it.icon}</span>
      ${state.railOpen ? `<span class="rail-label">${escapeHtml(it.label)}</span>` : ""}
      ${hasBadge && state.railOpen ? `<span class="rail-badge">${escapeHtml(String(badgeVal))}</span>` : ""}
      ${hasBadge && !state.railOpen ? `<span class="rail-dot"></span>` : ""}
    </button>
  `;
}

function bind() {
  railEl.querySelectorAll("[data-nav]").forEach((btn) => {
    btn.addEventListener("click", () => navigate(btn.getAttribute("data-nav")));
  });
  const toggle = railEl.querySelector("#railToggleBtn");
  if (toggle) toggle.addEventListener("click", () => setState({ railOpen: !state.railOpen }));

  const accountBtn = railEl.querySelector("#accountBtn");
  if (accountBtn) accountBtn.addEventListener("click", () => setState({ accountPanelOpen: true }));

  const signOutBtn = railEl.querySelector("#signOutBtn");
  if (signOutBtn)
    signOutBtn.addEventListener("click", async () => {
      await logout();
      window.dispatchEvent(new CustomEvent("erm:signed-out"));
    });

  const themeBtn = topbarEl.querySelector("#topThemeBtn");
  themeBtn.addEventListener("click", () => {
    const next = getTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    setState({ theme: next });
  });

  const goReview = topbarEl.querySelector("#goReviewBtn");
  goReview.addEventListener("click", () => navigate("review"));
}

function todayLabel() {
  const d = new Date();
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

// Opportunistically populate the nav "Review queue" badge / header
// export-shortcut count (`pendingCount`/`approvedCount`, §2) from the
// dashboard summary endpoint, if it exists. Silent no-op on failure —
// this is shell chrome, not worth an error banner if the (currently
// stub) dashboard API isn't wired yet.
async function refreshShellCounts() {
  try {
    const data = await api.get("/dashboard/today");
    if (data && typeof data === "object") {
      const patch = {};
      if ("pendingCount" in data) patch.pendingCount = data.pendingCount;
      if ("approvedCount" in data) patch.approvedCount = data.approvedCount;
      if (Object.keys(patch).length) setState(patch);
    }
  } catch (err) {
    // expected while /dashboard/today is a stub — no route registered yet.
  }
}

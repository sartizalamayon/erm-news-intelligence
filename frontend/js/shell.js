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
    <div class="app-shell">
      <aside id="railEl" class="rail"></aside>
      <main class="main-area">
        <div id="topbarEl" class="topbar"></div>
        <div id="screenRoot"></div>
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
  return { navigate };
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

export function navigate(screenKey, opts = {}) {
  setState({ screen: screenKey, ...opts });
}

function pageTitleFor() {
  if (state.screen === "dash") {
    return [
      "Dashboard",
      state.dashMode === "short" ? "Short term · today and this fortnight" : "Long term · twelve-month trends",
    ];
  }
  return SCREEN_TITLES[state.screen] || ["", ""];
}

// Mounts/unmounts the active screen into the stable #screenRoot node.
// Only runs the (potentially expensive, data-fetching) mount when the
// screen or dashboard tab actually changed — every other state change
// (theme, rail, counts ticking in) leaves the mounted screen alone.
function syncScreen() {
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
              <button id="accountBtn" class="rail-signout" title="Account & security">&#9881;</button>
              <button id="signOutBtn" class="rail-signout" title="Sign out">&#9211;</button>`
            : ""
        }
      </div>
      <button id="railToggleBtn" class="rail-toggle">${state.railOpen ? "‹ Collapse" : "›"}</button>
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
            <button id="stopRunBtn" class="btn" style="border-radius:999px;background:var(--redsoft);color:var(--red);font-size:11px;font-weight:600;padding:4px 10px;">Stop</button>
          </div>`
        : ""
    }
    <span class="today-label">${escapeHtml(todayLabel())}</span>
    <button id="topThemeBtn" class="icon-btn" title="Switch theme">${themeGlyph(getTheme())}</button>
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

  const stopRun = topbarEl.querySelector("#stopRunBtn");
  if (stopRun)
    stopRun.addEventListener("click", () => {
      setState({ progress: "" });
    });
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

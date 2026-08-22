// URL <-> screen mapping, using the History API.
//
// Navigation used to live only in `state.screen`, in memory. That meant
// no screen had an address: you could not link a colleague to the
// Review queue, the browser Back button did nothing (or left the app
// entirely), and any refresh — including the one after a session
// expiry — dumped you back on the Dashboard regardless of where you
// were. For a tool people keep open all day, that is the difference
// between an app and a demo.
//
// Deliberately tiny and framework-free, matching the rest of this
// frontend: a screen key maps to `/<key>`, plus one optional sub-mode
// for the Dashboard's short/long tabs. Everything below the top-level
// screen (filters, selections, drafts) stays in screen-local state, as
// it always has — this module only owns "which screen, which mode".
//
// Every function here is PURE except `readLocation`/`pushRoute`, so the
// mapping is unit-testable without a DOM (see tests/frontend/).

// The screen keys that may appear in a URL. An unknown or unroutable
// path falls back to `dash` rather than leaving the app blank — a
// hand-typed or stale URL must never produce a dead screen.
export const ROUTABLE_SCREENS = [
  "dash",
  "review",
  "export",
  "search",
  "run",
  "sources",
  "keywords",
  "prompts",
  "models",
  "manual",
  "users",
];

/**
 * Paths that no longer have a screen of their own, and where they went.
 *
 * `/health` was the Source health screen. It is now part of Sources, and
 * its pipeline-stage controls moved to Run collection. The path stays
 * resolvable because people bookmark admin pages and paste them to each
 * other: dropping it outright would send every existing link to the
 * Dashboard with no explanation. `screenForPath` resolves these to the
 * new screen, and the shell then rewrites the address bar (replaceState,
 * so Back does not walk into the dead path).
 *
 * Delete an entry once the old link is safely out of circulation.
 */
export const LEGACY_PATHS = { health: "sources" };

export const DEFAULT_SCREEN = "dash";
export const DASH_MODES = ["short", "long"];
export const DEFAULT_DASH_MODE = "short";

/**
 * The Long-term view is four sub-tabs, each addressable as a third path
 * segment (`/dash/long/economy`). They are separately linkable for the
 * same reason the Long-term tab itself is: someone reads a number on the
 * Economy board, and the way they hand it to a colleague is the URL.
 *
 * `risk` is the landing tab and stays implicit — `/dash/long` and
 * `/dash/long/risk` are the same view, and the shorter one is what the
 * app writes back to the address bar.
 */
export const LONG_TABS = ["risk", "market", "economy", "telecom"];
export const DEFAULT_LONG_TAB = "risk";

/**
 * Build the path for a screen. The Dashboard carries its tab as a
 * second segment (`/dash/long`) so the long-term view is linkable too;
 * `short` is the default and stays implicit to keep the common URL clean.
 *
 * @param {string} screen
 * @param {{ dashMode?: string }} [opts]
 * @returns {string} an absolute path, always starting with "/"
 */
export function pathForScreen(screen, opts = {}) {
  const key = ROUTABLE_SCREENS.includes(screen) ? screen : DEFAULT_SCREEN;
  if (key === "dash" && opts.dashMode === "long") {
    const tab = opts.longTab;
    // DEFAULT_LONG_TAB is deliberately not spelled out in the URL, so
    // the tab a reader lands on has one canonical address rather than
    // two that render identically.
    if (tab && tab !== DEFAULT_LONG_TAB && LONG_TABS.includes(tab)) return `/dash/long/${tab}`;
    return "/dash/long";
  }
  return `/${key}`;
}

/**
 * Parse a pathname into a screen + mode. Never throws and never returns
 * an unroutable screen: garbage in yields the Dashboard, because a bad
 * URL should land somewhere useful rather than nowhere.
 *
 * @param {string} pathname
 * @returns {{ screen: string, dashMode: string }}
 */
export function screenForPath(pathname) {
  const parts = String(pathname || "")
    .split("/")
    .filter(Boolean);
  const [head, sub, tab] = parts;
  if (head && Object.hasOwn(LEGACY_PATHS, head)) {
    return { screen: LEGACY_PATHS[head], dashMode: DEFAULT_DASH_MODE, longTab: DEFAULT_LONG_TAB };
  }
  if (!head || !ROUTABLE_SCREENS.includes(head)) {
    return { screen: DEFAULT_SCREEN, dashMode: DEFAULT_DASH_MODE, longTab: DEFAULT_LONG_TAB };
  }
  if (head === "dash") {
    const dashMode = DASH_MODES.includes(sub) ? sub : DEFAULT_DASH_MODE;
    // An unknown third segment falls back to the landing tab rather than
    // rendering nothing, for the same reason an unknown screen falls back
    // to the Dashboard: a bad URL should land somewhere useful.
    const longTab = dashMode === "long" && LONG_TABS.includes(tab) ? tab : DEFAULT_LONG_TAB;
    return { screen: "dash", dashMode, longTab };
  }
  return { screen: head, dashMode: DEFAULT_DASH_MODE, longTab: DEFAULT_LONG_TAB };
}

/**
 * Whether this screen may be shown to this user. Admin screens are
 * gated here as well as in the sidebar, because a URL is a way in that
 * the sidebar is not: without this, typing /sources as a non-admin
 * would mount an admin screen that then fails its API calls with 403s
 * and renders as a broken page rather than a redirect.
 *
 * The real enforcement is server-side (`require_admin` on every admin
 * route); this only decides what to render.
 *
 * @param {string} screen
 * @param {{ is_admin?: boolean } | null} user
 */
export function canAccess(screen, user) {
  const adminOnly = new Set(["run", "sources", "keywords", "prompts", "models", "users"]);
  if (!adminOnly.has(screen)) return true;
  return !!(user && user.is_admin);
}

/** Current screen/mode from `window.location`. */
export function readLocation(loc = typeof window !== "undefined" ? window.location : null) {
  if (!loc) return { screen: DEFAULT_SCREEN, dashMode: DEFAULT_DASH_MODE, longTab: DEFAULT_LONG_TAB };
  return screenForPath(loc.pathname);
}

/**
 * Reflect a screen change in the address bar.
 *
 * `replace` is used when the app is CORRECTING the URL rather than
 * responding to a user action (an unknown path, or an admin screen a
 * non-admin asked for). Pushing in those cases would put the bad URL in
 * the history stack, so Back would walk straight back into it.
 */
export function pushRoute(screen, opts = {}, { replace = false } = {}) {
  if (typeof window === "undefined" || !window.history) return;
  const path = pathForScreen(screen, opts);
  const current = window.location.pathname;
  if (current === path && !replace) return; // don't stack duplicate entries
  const url = path + window.location.search + window.location.hash;
  if (replace) window.history.replaceState({ screen }, "", url);
  else window.history.pushState({ screen }, "", url);
}

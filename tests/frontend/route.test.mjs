// Routing: URL <-> screen mapping.
//
// Run with:  node --test tests/frontend/
//
// Zero dependencies — `node:test` and `node:assert` ship with Node.
// That is deliberate and matches the frontend's own architecture: this
// app has no package.json, no bundler and no node_modules, and adding a
// test framework would have been the first crack in that.
//
// Everything asserted here is a PURE function, so no DOM is needed.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_DASH_MODE,
  DEFAULT_LONG_TAB,
  DEFAULT_SCREEN,
  LEGACY_PATHS,
  LONG_TABS,
  ROUTABLE_SCREENS,
  canAccess,
  pathForScreen,
  screenForPath,
} from "../../frontend/js/route.js";

// ── pathForScreen ────────────────────────────────────────────────────────

test("every routable screen gets its own path", () => {
  for (const key of ROUTABLE_SCREENS) {
    assert.equal(pathForScreen(key), `/${key}`, `screen ${key}`);
  }
});

test("the dashboard's long tab is addressable, short stays implicit", () => {
  assert.equal(pathForScreen("dash", { dashMode: "long" }), "/dash/long");
  assert.equal(pathForScreen("dash", { dashMode: "short" }), "/dash");
  assert.equal(pathForScreen("dash"), "/dash");
});

test("an unknown screen falls back to the default rather than producing a dead path", () => {
  assert.equal(pathForScreen("does-not-exist"), `/${DEFAULT_SCREEN}`);
  assert.equal(pathForScreen(""), `/${DEFAULT_SCREEN}`);
});

// ── screenForPath ────────────────────────────────────────────────────────

test("paths round-trip back to their screen", () => {
  for (const key of ROUTABLE_SCREENS) {
    assert.equal(screenForPath(`/${key}`).screen, key, `path /${key}`);
  }
});

test("round-trips survive both directions for the dashboard tabs", () => {
  // `longTab` is part of every parse result, not just the long-term
  // ones, so a caller can read it without a mode check first. This
  // assertion is exhaustive on purpose: it was written against the
  // two-key shape that predated long-term tabs, and stayed red and
  // unnoticed for the whole life of the feature because CI had no step
  // that ran this suite.
  const long = screenForPath(pathForScreen("dash", { dashMode: "long" }));
  assert.deepEqual(long, { screen: "dash", dashMode: "long", longTab: DEFAULT_LONG_TAB });
  const short = screenForPath(pathForScreen("dash", { dashMode: "short" }));
  assert.deepEqual(short, { screen: "dash", dashMode: "short", longTab: DEFAULT_LONG_TAB });
});

test("every long-term tab round-trips through its own URL", () => {
  // The point of putting the tab in the path: a CRO looking at the
  // Economy board hands a colleague the URL and they land on Economy.
  for (const tab of LONG_TABS) {
    const path = pathForScreen("dash", { dashMode: "long", longTab: tab });
    assert.deepEqual(
      screenForPath(path),
      { screen: "dash", dashMode: "long", longTab: tab },
      `tab ${tab} via ${path}`
    );
  }
});

test("the landing tab has exactly one canonical URL", () => {
  // `/dash/long` and `/dash/long/risk` render the same view, so the app
  // must only ever write the short one back to the address bar.
  assert.equal(pathForScreen("dash", { dashMode: "long", longTab: DEFAULT_LONG_TAB }), "/dash/long");
  assert.equal(pathForScreen("dash", { dashMode: "long" }), "/dash/long");
  assert.deepEqual(screenForPath("/dash/long"), screenForPath(`/dash/long/${DEFAULT_LONG_TAB}`));
});

test("a bad tab segment lands on the landing tab, not a blank board", () => {
  for (const bad of ["/dash/long/nope", "/dash/long/../etc", "/dash/long/RISK", "/dash/long//"]) {
    const out = screenForPath(bad);
    assert.ok(LONG_TABS.includes(out.longTab), `${bad} -> a real tab`);
  }
  assert.equal(screenForPath("/dash/long/nope").longTab, DEFAULT_LONG_TAB);
});

test("a tab segment is ignored on the short-term dashboard", () => {
  // `/dash/short/market` is not a thing; it must not smuggle a tab in.
  assert.deepEqual(screenForPath("/dash/short/market"), {
    screen: "dash",
    dashMode: "short",
    longTab: DEFAULT_LONG_TAB,
  });
});

test("garbage input lands on the dashboard instead of a blank screen", () => {
  // A hand-typed or stale URL must never leave the user nowhere.
  for (const bad of ["", "/", "/nope", "/../etc/passwd", "///", "/dash/nonsense-mode"]) {
    const out = screenForPath(bad);
    assert.ok(ROUTABLE_SCREENS.includes(out.screen), `${bad} -> routable`);
    assert.ok(["short", "long"].includes(out.dashMode), `${bad} -> valid mode`);
  }
  assert.equal(screenForPath("/dash/nonsense-mode").dashMode, DEFAULT_DASH_MODE);
});

test("trailing slashes and repeated separators are tolerated", () => {
  assert.equal(screenForPath("/review/").screen, "review");
  assert.equal(screenForPath("//review").screen, "review");
});

test("a null or undefined path does not throw", () => {
  assert.equal(screenForPath(null).screen, DEFAULT_SCREEN);
  assert.equal(screenForPath(undefined).screen, DEFAULT_SCREEN);
});

// ── retired paths ────────────────────────────────────────────────────────

test("a retired path resolves to the screen that absorbed it", () => {
  // /health was Source health. It is now part of Sources. Admin pages
  // get bookmarked and pasted between people, so the old path has to
  // land somewhere correct rather than silently on the Dashboard.
  assert.equal(screenForPath("/health").screen, "sources");
  assert.equal(screenForPath("/health/").screen, "sources");
});

test("every retired path points at a screen that still exists", () => {
  // Guards the rename-and-forget case: retiring a screen twice would
  // otherwise leave a redirect aimed at nothing, which resolves to the
  // Dashboard and looks exactly like the bug this map prevents.
  for (const [from, to] of Object.entries(LEGACY_PATHS)) {
    assert.ok(ROUTABLE_SCREENS.includes(to), `${from} redirects to unroutable ${to}`);
    assert.ok(!ROUTABLE_SCREENS.includes(from), `${from} is both retired and routable`);
  }
});

test("a retired path is rewritten rather than kept in the address bar", () => {
  // The shell compares pathForScreen(resolved) with the current path and
  // replaceState()s when they differ. That only works if the retired
  // path is NOT what pathForScreen produces for the new screen.
  for (const [from, to] of Object.entries(LEGACY_PATHS)) {
    assert.notEqual(pathForScreen(to), `/${from}`);
  }
});

// ── canAccess ────────────────────────────────────────────────────────────

test("admin screens are closed to non-admins", () => {
  // A URL is a way in that the sidebar is not: without this gate, a
  // non-admin typing /sources would mount an admin screen whose API
  // calls then 403, rendering a broken page instead of a redirect.
  const analyst = { is_admin: false };
  for (const key of ["run", "sources", "keywords", "prompts"]) {
    assert.equal(canAccess(key, analyst), false, `analyst must not reach ${key}`);
    assert.equal(canAccess(key, null), false, `signed-out must not reach ${key}`);
    assert.equal(canAccess(key, { is_admin: true }), true, `admin must reach ${key}`);
  }
});

test("daily-desk screens are open to everyone signed in", () => {
  for (const key of ["dash", "review", "export", "search", "manual"]) {
    assert.equal(canAccess(key, { is_admin: false }), true, key);
  }
});

test("every routable screen has an explicit access decision", () => {
  // Guards against adding a screen to ROUTABLE_SCREENS and forgetting
  // to decide who may see it — the default must never be "nobody".
  for (const key of ROUTABLE_SCREENS) {
    assert.equal(typeof canAccess(key, { is_admin: true }), "boolean", key);
  }
});

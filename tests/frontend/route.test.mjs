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
  DEFAULT_SCREEN,
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
  const long = screenForPath(pathForScreen("dash", { dashMode: "long" }));
  assert.deepEqual(long, { screen: "dash", dashMode: "long" });
  const short = screenForPath(pathForScreen("dash", { dashMode: "short" }));
  assert.deepEqual(short, { screen: "dash", dashMode: "short" });
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

// ── canAccess ────────────────────────────────────────────────────────────

test("admin screens are closed to non-admins", () => {
  // A URL is a way in that the sidebar is not: without this gate, a
  // non-admin typing /sources would mount an admin screen whose API
  // calls then 403, rendering a broken page instead of a redirect.
  const analyst = { is_admin: false };
  for (const key of ["run", "sources", "keywords", "prompts", "health"]) {
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

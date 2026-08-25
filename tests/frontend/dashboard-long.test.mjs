// The Long-term dashboard's two most expensive ways of being wrong:
// drawing a number that was never measured, and offering a control the
// server will refuse.
//
// Both had shipped. Neither was covered by anything.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { sparkHeightPct } from "../../frontend/js/screens/dashboard_long.js";

const SRC = readFileSync(new URL("../../frontend/js/screens/dashboard_long.js", import.meta.url), "utf8");

// Comment lines dropped, matching `copy.test.mjs`'s rule: a comment is
// not shown to anyone, and the comments in this file deliberately QUOTE
// the wrong copy they replaced so the next reader knows what went
// wrong. A copy check that cannot tell the quotation from the original
// would forbid explaining the bug.
const COPY = SRC.split("\n")
  .filter((line) => {
    const t = line.trim();
    return !(t.startsWith("//") || t.startsWith("*") || t.startsWith("/*"));
  })
  .join("\n");

// ── a zero month draws nothing ───────────────────────────────────────

test("a month with nothing in it draws no bar at all", () => {
  // `app/services/risklines.py::_normalise` floors its scaled series at
  // a true zero and says why in its docstring, in as many words: "a
  // month with no items should draw as nothing". Both sparkline strips
  // on this screen then rendered `Math.max(4, Math.round(v * 100))%`,
  // so every zero month drew a 4% stub.
  //
  // That is not a rounding quibble. These strips exist to show where
  // collection coverage stops: a run of empty months is the answer to
  // "how far back does this actually go", and a row of identical 4%
  // stubs turns that answer into a low but positive reading. The
  // backend went to the trouble of distinguishing "nothing" from
  // "nearly nothing" and the frontend threw the distinction away.
  assert.equal(sparkHeightPct(0), 0);
});

test("a small but real month is still visible", () => {
  // The other half, and the reason the floor is not simply deleted. A
  // month that scored 0.3% of the series maximum is a real measurement,
  // and `Math.round(0.003 * 100)` is 0 — so without a floor above zero
  // it would be hidden exactly as badly as a phantom bar shows what is
  // not there. Two different failures, one of which is easy to
  // introduce while fixing the other.
  assert.equal(sparkHeightPct(0.003), 4);
  assert.equal(sparkHeightPct(0.02), 4);
});

test("a full month fills the track and nothing overflows it", () => {
  assert.equal(sparkHeightPct(1), 100);
  assert.equal(sparkHeightPct(0.5), 50);
});

test("a missing or nonsensical value draws nothing rather than something", () => {
  // A null in the series is an absence, and the honest drawing of an
  // absence is the same as the drawing of a zero. A negative or a NaN
  // is a bug upstream, and a bug should not be able to produce a bar
  // that escapes its track.
  for (const bad of [null, undefined, NaN, -1, "", "abc"]) {
    assert.equal(sparkHeightPct(bad), 0, `${String(bad)} should draw nothing`);
  }
});

// ── the watchlist add flow is admin-only ─────────────────────────────

test("the watchlist card is told whether the reader is an admin", () => {
  assert.match(
    SRC,
    /function watchlistCard\(wl, isAdmin\)/,
    "watchlistCard must take the admin flag; without it the card cannot gate anything"
  );
});

test("nothing on the watchlist promises that anyone can add an entity", () => {
  // GET /dashboard/long/telecom requires only `get_current_user`, so
  // every analyst reaches this screen. POST /watchlist/preview, POST
  // /watchlist and DELETE /watchlist/{id} all sit behind
  // `require_admin`. The card's blurb used to read "Anyone can add
  // one", which is the opposite of true and was written directly above
  // a button that took an analyst through a five-field form to a red
  // "Admin role required." banner.
  assert.doesNotMatch(COPY, /Anyone can add/i, "the API refuses this for a non-admin; the copy must not offer it");
});

test("the add button, the add panel and the delete control are all behind the admin flag", () => {
  // Presentation only — `require_admin` on the routes is the actual
  // enforcement, exactly as `route.js::canAccess` is presentation for
  // the admin screens. What this prevents is a screen that invites work
  // the server will throw away.
  for (const [hook, what] of [
    ['id="wlAddBtn"', "the Add an entity button"],
    ["addPanelHtml(groups)", "the add form"],
    ["js-wl-del", "the remove control on a custom entity chip"],
  ]) {
    const at = SRC.indexOf(hook);
    assert.ok(at > 0, `${what} (${hook}) not found`);
    // The nearest `isAdmin` test above it, within the conditional that
    // wraps it. A generous window, because the gate is a ternary that
    // can sit several formatted lines up.
    const before = SRC.slice(Math.max(0, at - 400), at);
    assert.match(before, /isAdmin/, `${what} renders without an isAdmin gate above it`);
  }
});

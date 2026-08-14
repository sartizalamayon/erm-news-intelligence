// `frontend/js/util.js` — the shared pure helpers every screen renders
// through. Zero dependencies: `node:test` ships with Node.
//
// `escapeHtml` gets the most attention here because it is the ONLY
// thing standing between remote text and `innerHTML`. Article titles,
// source names and provider error strings all come from outside this
// system and all end up interpolated into markup; if this function is
// wrong, every screen is wrong at once.

import { test } from "node:test";
import assert from "node:assert/strict";

import { escapeHtml, formatNumber, pctOfMax, pctOfTotal, poly, tier } from "../../frontend/js/util.js";

// ── escapeHtml ───────────────────────────────────────────────────────────

test("escapes every character that can break out of markup", () => {
  assert.equal(escapeHtml("<script>"), "&lt;script&gt;");
  assert.equal(escapeHtml('a "b" c'), "a &quot;b&quot; c");
  assert.equal(escapeHtml("a & b"), "a &amp; b");
  assert.equal(escapeHtml("it's"), "it&#39;s");
});

test("neutralises a real injection attempt", () => {
  const attack = `"><img src=x onerror=alert(1)>`;
  const escaped = escapeHtml(attack);
  // The point is not the exact entity spelling — it is that no
  // character able to open a tag or close an attribute survives.
  assert.ok(!escaped.includes("<"), "no raw <");
  assert.ok(!escaped.includes(">"), "no raw >");
  assert.ok(!escaped.includes('"'), "no raw double quote");
});

test("ampersand is escaped FIRST so entities are not double-broken", () => {
  // If & were escaped after <, "&lt;" would become "&amp;lt;".
  assert.equal(escapeHtml("<"), "&lt;");
  assert.equal(escapeHtml("&lt;"), "&amp;lt;");
});

test("handles the values that actually show up in this app", () => {
  // Bengali titles must pass through untouched — escaping must not
  // mangle non-Latin script, which is 72% of this corpus.
  assert.equal(escapeHtml("গ্যাস সংকটে দুঃখ প্রকাশ"), "গ্যাস সংকটে দুঃখ প্রকাশ");
  assert.equal(escapeHtml(""), "");
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(undefined), "");
  assert.equal(escapeHtml(0), "0");
});

// ── tier: relevance score -> visual band ─────────────────────────────────

test("score bands cover every integer 1-10 with no gaps", () => {
  const seen = new Set();
  for (let score = 1; score <= 10; score++) {
    const t = tier(score);
    assert.ok(t, `score ${score} must map to a tier`);
    seen.add(typeof t === "string" ? t : t.key ?? JSON.stringify(t));
  }
  assert.ok(seen.size >= 2, "bands must actually differentiate scores");
});

// ── number helpers ───────────────────────────────────────────────────────

// These return CSS-ready STRINGS ("100%"), not numbers — they are
// interpolated straight into `style="width:..."`.
const pctNum = (s) => Number(String(s).replace("%", ""));

test("pctOfMax never exceeds 100 or goes negative", () => {
  const list = [0, 5, 10];
  assert.equal(pctOfMax(10, list), "100%");
  assert.equal(pctOfMax(0, list), "0%");
  // A value larger than anything in the list must still clamp: an
  // unclamped ratio yielded "9990%", which is not a wrong number so
  // much as a bar escaping its own track.
  assert.ok(pctNum(pctOfMax(999, list)) <= 100, pctOfMax(999, list));
  assert.ok(pctNum(pctOfMax(-5, list)) >= 0);
});

test("pctOfMax survives an empty or missing list instead of dividing by zero", () => {
  assert.equal(pctOfMax(5, []), "0%");
  assert.equal(pctOfMax(5, null), "0%");
});

test("pctOfTotal returns 0% rather than NaN when the total is 0", () => {
  assert.equal(pctOfTotal(0, 0), "0%");
  assert.equal(pctOfTotal(5, 0), "0%");
  assert.ok(pctNum(pctOfTotal(999, 10)) <= 100);
});

test("formatNumber groups thousands", () => {
  assert.equal(String(formatNumber(1560)).replace(/ /g, ","), "1,560");
  assert.equal(String(formatNumber(0)), "0");
});

// ── poly: sparkline point generation ─────────────────────────────────────

test("poly emits one point per value and never NaN", () => {
  const pts = poly([1, 5, 3, 9], 100, 26, false);
  const pairs = pts.trim().split(/\s+/);
  assert.equal(pairs.length, 4);
  assert.ok(!/NaN/.test(pts), `sparkline contained NaN: ${pts}`);
});

test("poly handles a flat series without dividing by a zero range", () => {
  // All-equal values make max-min == 0; this used to be the classic
  // place a sparkline turns into NaN and silently disappears.
  const pts = poly([7, 7, 7], 100, 26, false);
  assert.ok(!/NaN/.test(pts), `flat series produced NaN: ${pts}`);
});

test("poly handles the empty and single-value cases", () => {
  assert.doesNotThrow(() => poly([], 100, 26, false));
  assert.doesNotThrow(() => poly([1], 100, 26, false));
});

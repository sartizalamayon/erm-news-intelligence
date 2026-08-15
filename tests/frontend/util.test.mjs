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

import {
  escapeHtml,
  fmtTimestamp,
  formatNumber,
  highlightTerms,
  pctOfMax,
  pctOfTotal,
  poly,
  searchTerms,
  snippetAround,
  tier,
} from "../../frontend/js/util.js";

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

// ── fmtTimestamp ─────────────────────────────────────────────────────────

test("fmtTimestamp turns an API timestamp into something readable", () => {
  // The run-history table printed the raw value, so every row read
  // "2026-08-14T23:36:05.053681+06:00" in a column whose only job is
  // to say which run this was.
  const out = fmtTimestamp("2026-08-14T23:36:05.053681+06:00");
  assert.ok(!out.includes("T"), `still ISO: ${out}`);
  assert.ok(!out.includes("053681"), `microseconds survived: ${out}`);
  assert.match(out, /Aug/);
  assert.match(out, /\d{2}:\d{2}/);
});

test("fmtTimestamp never renders Invalid Date where a time belongs", () => {
  // A blank cell is honest. "Invalid Date" looks like a crash.
  assert.equal(fmtTimestamp(""), "");
  assert.equal(fmtTimestamp(null), "");
  assert.equal(fmtTimestamp(undefined), "");
  for (const junk of ["not-a-date", "0000", "{}"]) {
    assert.ok(!/Invalid Date/.test(fmtTimestamp(junk)), junk);
  }
});

// ── search highlighting ──────────────────────────────────────────────────
//
// This is the one helper in the codebase that deliberately emits markup
// containing content derived from remote text, so it carries the whole
// weight of not being an XSS hole.

test("searchTerms splits, lowercases and dedupes", () => {
  assert.deepEqual(searchTerms("Hormuz OIL  oil "), ["hormuz", "oil"]);
  assert.deepEqual(searchTerms(""), []);
  assert.deepEqual(searchTerms(null), []);
});

test("highlightTerms marks the matches", () => {
  assert.equal(highlightTerms("Hormuz oil spill", ["oil"]), "Hormuz <mark>oil</mark> spill");
  assert.equal(highlightTerms("OIL prices", ["oil"]), "<mark>OIL</mark> prices", "match is case-insensitive, output keeps the original case");
});

test("highlightTerms escapes everything it emits", () => {
  const out = highlightTerms('<img src=x onerror=alert(1)> "quoted" & more', ["img"]);
  assert.ok(!out.includes("<img"), out);
  assert.ok(!out.includes("onerror=alert(1)>"), out);
  assert.match(out, /&lt;<mark>img<\/mark>/);
  // The only tags in the output are the <mark> pairs we added.
  const tags = out.match(/<[^>]*>/g) || [];
  assert.deepEqual([...new Set(tags)].sort(), ["</mark>", "<mark>"]);
});

test("highlightTerms does not corrupt HTML entities", () => {
  // The naive implementation escapes FIRST and then replaces, so
  // searching "amp" would inject a <mark> inside the "&amp;" produced
  // by escaping an ampersand. Matching on raw offsets cannot.
  assert.equal(highlightTerms("Tom & Jerry", ["amp"]), "Tom &amp; Jerry");
  assert.equal(highlightTerms("a < b", ["lt"]), "a &lt; b");
  assert.equal(highlightTerms("say \"hi\"", ["quot"]), "say &quot;hi&quot;");
});

test("highlightTerms merges overlapping matches instead of nesting tags", () => {
  // "oil" and "oils" both hit at index 0. Emitting both opens a second
  // <mark> before the first closes.
  const out = highlightTerms("oils rise", ["oil", "oils"]);
  assert.equal(out, "<mark>oils</mark> rise");
  assert.equal((out.match(/<mark>/g) || []).length, (out.match(/<\/mark>/g) || []).length);
});

test("highlightTerms handles nothing-to-do inputs without throwing", () => {
  assert.equal(highlightTerms("", ["a"]), "");
  assert.equal(highlightTerms(null, ["a"]), "");
  assert.equal(highlightTerms("plain", []), "plain");
  assert.equal(highlightTerms("plain", null), "plain");
  assert.equal(highlightTerms("plain", ["zzz"]), "plain");
  // An empty term would make indexOf return the cursor forever.
  assert.equal(highlightTerms("plain", ["", "  "]).includes("mark"), false);
});

test("highlightTerms still escapes when there is no match", () => {
  assert.equal(highlightTerms("<b>hi</b>", ["zzz"]), "&lt;b&gt;hi&lt;/b&gt;");
  assert.equal(highlightTerms("<b>hi</b>", []), "&lt;b&gt;hi&lt;/b&gt;");
});

test("snippetAround centres on the first match", () => {
  const body = "x".repeat(400) + " oil " + "y".repeat(400);
  const out = snippetAround(body, ["oil"], 30);
  assert.ok(out.includes("oil"), out);
  assert.ok(out.length < 120, `snippet should be short, got ${out.length}`);
  assert.ok(out.startsWith("…") && out.endsWith("…"), out);
});

test("snippetAround degrades sanely", () => {
  assert.equal(snippetAround("", ["oil"]), "");
  assert.equal(snippetAround(null, ["oil"]), "");
  // No match: show the opening rather than nothing.
  assert.ok(snippetAround("nothing relevant here", ["zzz"]).startsWith("nothing"));
  // Collapses the whitespace that makes scraped bodies unreadable.
  assert.equal(snippetAround("a\n\n   b", ["a"]), "a b");
});

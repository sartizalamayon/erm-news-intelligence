// The "How it works" page, and the difference between an example and a
// measurement.
//
// `screens/manual.js` is static explainer prose. Its walkthrough numbers
// are a worked example taken from the design, not readings from this
// database, and the page says so in most places: phase 1 is "Collect:
// about 5,000 articles a day (example day)", phase 2 is "Keyword filter:
// 5,000 down to about 1,100 (example day)", and phase 2's per-section
// cards carry their own caption.
//
// The gaps were the places it did NOT say so, and a partial labelling
// is worse than none: a reader who sees two headings explicitly marked
// "(example day)" reasonably concludes that the third, which is not
// marked, was measured. Phase 3 read "AI stage: about 1,100 organised
// into a reviewable set" with no label at all, and the page's own H2
// stated "Five thousand articles in, twenty-five out." as a flat fact.
// This is the orientation page. An analyst who checks a number here
// against the Dashboard and finds it does not match stops trusting the
// rest of the explanation too, which is the whole asset the page has.
//
// The third check is a different failure with the same shape: prose
// that hardcodes a count the application lets an administrator change.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = readFileSync(new URL("../../frontend/js/screens/manual.js", import.meta.url), "utf8");

const COPY = SRC.split("\n")
  .filter((line) => {
    const t = line.trim();
    return !(t.startsWith("//") || t.startsWith("*") || t.startsWith("/*"));
  })
  .join("\n");

test("every phase heading that quotes a volume says it is an example", () => {
  // `phaseHead(n, chip, title)` is the one place a phase title is
  // written, so every title can be read out of the call sites.
  const titles = [...COPY.matchAll(/phaseHead\(\s*\d+\s*,\s*"[^"]*"\s*,\s*"([^"]*)"/g)].map((m) => m[1]);
  assert.ok(titles.length >= 4, `expected the four phase headings, found ${titles.length}`);

  const offenders = titles.filter((t) => /\d/.test(t) && !/example day/i.test(t));
  assert.deepEqual(
    offenders,
    [],
    "a phase heading quoting a number must be labelled, or the unlabelled ones read as measured:\n  " +
      offenders.join("\n  ")
  );
});

test("the page headline does not state its funnel as today's fact", () => {
  const h2 = (COPY.match(/<h2[^>]*>([^<]*)<\/h2>/) || [])[1];
  assert.ok(h2, "the manual's H2 was not found");
  assert.match(
    h2,
    /typical|example|about|roughly|around/i,
    `the H2 quotes a count and needs the same hedge every phase heading carries: ${h2}`
  );
});

test("the manual does not hardcode how many risk sections there are", () => {
  // Sections are admin-configurable free text: an admin adds a
  // "Competitors" section on the Keywords screen with no code change.
  // `frontend/js/util.js` records at length what the same hardcoded
  // five cost when they were used as a FALLBACK — approved articles in
  // an admin-created section silently missing from every export that
  // went out — and a sentence stating the count is the same mistake in
  // a cheaper place: this page would tell an analyst there are five
  // while the Dashboard beside it shows six.
  //
  // The five example cards stay, because a worked example needs labels.
  // What may not stay is prose committing to the number.
  const offenders = [];
  COPY.split("\n").forEach((line, i) => {
    if (/\b(two|three|four|five|six|seven|eight|nine|ten)\s+risk sections\b/i.test(line)) {
      offenders.push(`manual.js:${i + 1}  ${line.trim().slice(0, 100)}`);
    }
  });
  assert.deepEqual(
    offenders,
    [],
    "the section list is admin-configured, so prose must not fix its length:\n  " + offenders.join("\n  ")
  );
});

test("the illustrative section cards are captioned as illustrative", () => {
  // The counts were already captioned. The five section NAMES beside
  // them were not, and they drift for exactly the same reason.
  assert.match(COPY, /illustrative/i, "the example split must say it is an example");
  assert.match(
    COPY,
    /example set of sections/i,
    "the caption must cover the section names, not only their counts"
  );
});

// `components/toast.js` — the shared result banner.
//
// This component exists because the same markup was hand-written twice
// and the copies drifted: health.js's was fixed to be viewport-pinned
// after a result rendered ~1,554px off-screen (a working button looking
// dead), while sources.js kept the broken inline version — on a page
// that also lists 51 sources each with a test button.
//
// These tests pin the two properties that bug was about: the result is
// escaped, and it is positioned where a user can actually see it.

import { test } from "node:test";
import assert from "node:assert/strict";

import { readFileSync } from "node:fs";

import { resultToast } from "../../frontend/js/components/toast.js";

test("renders nothing when there is nothing to say", () => {
  assert.equal(resultToast({ message: null }), "");
  assert.equal(resultToast({ message: "" }), "");
  assert.equal(resultToast({ message: undefined }), "");
});

test("success and failure are visually distinguishable", () => {
  assert.match(resultToast({ message: "ok", ok: true }), /banner-ok/);
  assert.match(resultToast({ message: "bad", ok: false }), /banner-error/);
});

test("the message is escaped", () => {
  // The message is built from a source NAME (admin-typed) and a
  // provider/source DETAIL string — both remote.
  const html = resultToast({ message: `<img src=x onerror=alert(1)>` });
  assert.ok(!html.includes("<img"), "must not emit a raw tag");
  assert.match(html, /&lt;img/);
});

test("the dismiss id is escaped too", () => {
  // Interpolated into an attribute; a quote here would break out of it.
  const html = resultToast({ message: "hi", dismissId: 'x" onclick="evil()' });
  assert.ok(!html.includes('onclick="evil()'), "must not allow attribute injection");
});

test("is pinned to the viewport, not placed in document flow", () => {
  // The whole point of the component. An in-flow banner above a 51-row
  // table is invisible for exactly the rows a user is most likely to be
  // testing.
  const html = resultToast({ message: "done" });
  assert.match(html, /position:fixed/);
  assert.match(html, /bottom:\s*24px/);
});

test("is announced to assistive technology", () => {
  const html = resultToast({ message: "done" });
  assert.match(html, /role="status"/);
  assert.match(html, /aria-live="polite"/);
});

test("both screens that show a test result use the shared component", () => {
  // Guards the actual regression: two hand-written copies drifting, so
  // a fix landed in one and not the other.
  for (const rel of ["health.js", "sources.js"]) {
    const src = readFileSync(new URL(`../../frontend/js/screens/${rel}`, import.meta.url).pathname, "utf8");
    assert.ok(src.includes("resultToast("), `${rel} must render via the shared toast`);
    assert.ok(
      !/class="banner \$\{s\.testOk/.test(src),
      `${rel} still hand-rolls the banner — that is how the two copies drifted`
    );
  }
});

test("test-result messages prefer the server's sentence instead of rebuilding it", () => {
  // This exact bug appeared THREE times: `res.detail` is already the
  // human sentence ("102 fetched, 25 from today (via rss_multi)"), and
  // both screens rebuilt it and then appended detail, printing the same
  // clause twice in one banner. It was fixed in health.js, then found
  // again in sources.js because the two were written by copy-paste.
  //
  // The correct shape is `res.detail || <fallback>`. The broken shape
  // concatenates: `... ${res.detail ? " · " + res.detail : ""}`.
  for (const rel of ["health.js", "sources.js"]) {
    const src = readFileSync(new URL(`../../frontend/js/screens/${rel}`, import.meta.url).pathname, "utf8");
    assert.ok(
      !/res\.detail\s*\?\s*"\s*·\s*"\s*\+\s*res\.detail/.test(src),
      `${rel} appends res.detail to a rebuilt sentence — that prints the same clause twice`
    );
    assert.match(src, /res\.detail\s*\|\|/, `${rel} should prefer res.detail`);
  }
});

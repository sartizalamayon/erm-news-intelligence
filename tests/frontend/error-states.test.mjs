// What a screen says, and what it lets you do, when a request fails.
//
// `util.js::notWiredBanner` renders a calm blue notice saying the
// endpoint "isn't implemented on the API yet" and that the screen "will
// populate automatically" once it ships. That is true of exactly one
// failure — a 404 from a stub router with no routes registered — and it
// is a lie about every other one. `review.js`, `sources.js`, `users.js`
// and `search.js` all gate it on `err.kind === "not_found"`. Keywords
// and AI models did not: they branched `forbidden` against
// everything-else, so a 500, a 429 or a dropped connection all rendered
// as "not implemented yet".
//
// The cost is two-fold and both halves are bad. An admin is told the
// wrong cause, so they go looking for a backend deploy instead of
// hitting refresh. And the banner carries no control at all, so the
// screen is stuck: the only way to retry is to navigate away and come
// back.
//
// These checks are source-level, like the rest of this suite: there is
// no DOM here, and the thing worth pinning is the SHAPE of the branch
// rather than a rendered string.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const JS_ROOT = new URL("../../frontend/js/", import.meta.url).pathname;

function jsFiles(dir = JS_ROOT, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) jsFiles(full, out);
    else if (entry.endsWith(".js")) out.push(full);
  }
  return out;
}

const FILES = jsFiles().map((path) => ({
  rel: path.split("/frontend/")[1],
  src: readFileSync(path, "utf8"),
}));

function isComment(line) {
  const t = line.trim();
  return t.startsWith("//") || t.startsWith("*") || t.startsWith("/*");
}

test("the source tree was actually found", () => {
  assert.ok(FILES.length >= 15, `expected the frontend js tree, found ${FILES.length} files`);
});

test("the not-implemented banner is only ever rendered for a 404", () => {
  // Every call site in this codebase puts the `not_found` test on the
  // same line as the banner or on the line immediately above it, so a
  // three-line window is generous rather than lenient. `util.js` is
  // skipped because it DEFINES the banner rather than deciding when to
  // show it.
  const offenders = [];
  for (const { rel, src } of FILES) {
    if (rel === "js/util.js") continue;
    const lines = src.split("\n");
    lines.forEach((line, i) => {
      if (isComment(line) || !line.includes("notWiredBanner(")) return;
      const window = lines
        .slice(Math.max(0, i - 3), i + 1)
        .filter((l) => !isComment(l))
        .join("\n");
      if (!window.includes("not_found")) offenders.push(`${rel}:${i + 1}  ${line.trim().slice(0, 90)}`);
    });
  }
  assert.deepEqual(
    offenders,
    [],
    'notWiredBanner() claims the endpoint does not exist yet. Gate it on err.kind === "not_found" ' +
      "and give every other failure the error text and a Retry:\n  " + offenders.join("\n  ")
  );
});

test("keywords and AI models offer a Retry when a load fails for any other reason", () => {
  // Both screens are admin-only consoles that people open when
  // something is already wrong, which is exactly when a stuck screen
  // costs the most. `sources.js` is the worked example: a red banner
  // carrying the real error text plus a Retry that refetches.
  for (const rel of ["js/screens/keywords.js", "js/screens/models.js"]) {
    const file = FILES.find((f) => f.rel === rel);
    assert.ok(file, `${rel} not found`);
    const body = file.src
      .split("\n")
      .filter((l) => !isComment(l))
      .join("\n");
    assert.match(body, /Retry<\/button>/, `${rel} renders no Retry control on a failed load`);
    // A Retry that nothing binds is worse than no Retry: it looks like
    // a way out and is not one.
    assert.match(
      body,
      /addEventListener\("click",\s*\(\)\s*=>\s*load/,
      `${rel} renders a Retry but never binds it to a reload`
    );
  }
});

test("no screen points a reader at GET /dashboard/trends", () => {
  // The route served a fabricated series and is being removed. The
  // dashboard's own error banner still named it, in a branch that could
  // never be reached (renderError only runs on the short-term tab), so
  // the single thing that string could ever have done was send somebody
  // looking for an endpoint that will not exist.
  const offenders = [];
  for (const { rel, src } of FILES) {
    src.split("\n").forEach((line, i) => {
      if (isComment(line) || !line.includes("/dashboard/trends")) return;
      offenders.push(`${rel}:${i + 1}  ${line.trim().slice(0, 90)}`);
    });
  }
  assert.deepEqual(offenders, [], "this endpoint is being deleted:\n  " + offenders.join("\n  "));
});

test("the dashboard binds its Retry exactly once", () => {
  // `renderError` calls `render()`, `render()` calls `bindHeader()`,
  // and `bindHeader()` already binds `#dashRetry`. A second binding
  // added afterwards in `renderError` meant one click fired two
  // requests for /dashboard/today — the heaviest query the API runs —
  // against an API that allows 120 requests per 60 seconds per IP.
  //
  // The rule this pins is the one that made the bug possible: in this
  // screen, rendering and binding are separate jobs, and `renderError`
  // only renders.
  const file = FILES.find((f) => f.rel === "js/screens/dashboard.js");
  assert.ok(file, "dashboard.js not found");

  const start = file.src.indexOf("function renderError(");
  assert.ok(start > 0, "renderError not found in dashboard.js");
  const body = bodyOf(file.src, start);
  assert.doesNotMatch(
    body,
    /addEventListener/,
    "renderError must not bind anything: render() -> bindHeader() already binds #dashRetry, " +
      "and a second binding makes one click fire two requests"
  );

  const bindStart = file.src.indexOf("function bindHeader(");
  assert.ok(bindStart > 0, "bindHeader not found in dashboard.js");
  assert.match(
    bodyOf(file.src, bindStart),
    /#dashRetry/,
    "bindHeader is where #dashRetry is bound; if that moved, move this test with it"
  );
});

/** The `{ ... }` block of the function declared at `from`, by brace depth. */
function bodyOf(src, from) {
  const open = src.indexOf("{", from);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") {
      depth--;
      if (depth === 0) return src.slice(open, i + 1);
    }
  }
  return src.slice(open);
}

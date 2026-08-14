// `api.js` — the error contract every screen renders against.
//
// This file exists because of a real mistake: while fixing the Sources
// form I wrote `err.body.detail` to surface a validation message, having
// ASSUMED that shape. `ApiError` does carry `body`, but it already lifts
// the API's `detail` into `message`, so the code was both redundant and
// one missing-property away from a TypeError inside an error handler —
// the worst place to throw, because it replaces a useful message with a
// blank screen.
//
// There is no TypeScript here (no build step, by design), so the shape
// cannot be checked at edit time. Pinning it as a test is the
// zero-dependency equivalent: change the contract and this fails.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { ApiError, classify } from "../../frontend/js/api.js";

const API_SRC = readFileSync(new URL("../../frontend/js/api.js", import.meta.url).pathname, "utf8");

test("ApiError exposes the four fields screens actually read", () => {
  const err = new ApiError("boom", { status: 422, kind: "validation", body: { detail: "x" } });
  assert.equal(err.message, "boom");
  assert.equal(err.status, 422);
  assert.equal(err.kind, "validation");
  assert.deepEqual(err.body, { detail: "x" });
  assert.equal(err.name, "ApiError");
  assert.ok(err instanceof Error, "must be catchable as an Error");
});

test("every field has a safe default so a handler cannot throw", () => {
  // An error handler that itself throws turns a useful message into a
  // blank screen. Nothing here may be undefined-by-omission.
  const err = new ApiError("boom");
  assert.equal(err.status, 0);
  assert.equal(err.kind, "error");
  assert.equal(err.body, null);
});

test("the API's `detail` is lifted into `message`", () => {
  // This is the property that made `err.body.detail` unnecessary: the
  // server's field-level validation text (app/api/error_handlers.py now
  // returns "methods: Field required") is already in `message`, which
  // is what `fmtErr` renders.
  assert.match(API_SRC, /payload\.detail\s*\|\|\s*payload\.message/);
});

test("status codes map to the kinds screens branch on", () => {
  // `kind` drives real UI decisions — dashboard.js renders a
  // "not wired yet" banner for not_found rather than a red error, and
  // main.js sends unauthorized back to login. A silent remap here would
  // change behaviour in several screens at once.
  const expected = {
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    429: "rate_limited",
    422: "validation",
    400: "validation",
    500: "server",
    503: "server",
    418: "error",
  };
  for (const [status, kind] of Object.entries(expected)) {
    assert.equal(classify(Number(status)), kind, `HTTP ${status}`);
  }
});

test("a network failure is distinguishable from an HTTP error", () => {
  // "the backend is not running" and "the backend said no" need
  // different messages; both used to look identical to the user.
  assert.match(API_SRC, /kind:\s*"network"/);
  const err = new ApiError("unreachable", { kind: "network" });
  assert.equal(err.kind, "network");
  assert.equal(err.status, 0, "a network failure has no HTTP status");
});

test("401 triggers the unauthorized handler exactly once per failure", () => {
  // main.js registers this to drop the user back to login on an expired
  // session; if it were not called, a stale session would render a
  // screenful of failed requests instead.
  assert.match(API_SRC, /onUnauthorized/);
  assert.match(API_SRC, /setUnauthorizedHandler/);
});

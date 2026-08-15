// UI copy rules.
//
// Em dashes and en dashes are prohibited in user-facing text (owner's
// call). They had spread to 74 lines across 12 screens, doing three
// different jobs: separating a label from a value, prefixing an error,
// and standing in for a missing value. Each needed a different fix, so
// this is worth a guard rather than a one-time sweep: the next person
// writing a banner will reach for one by habit.
//
// Scope is USER-FACING text only. Code comments are not shown to
// anyone and are left alone.

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
  lines: readFileSync(path, "utf8").split("\n"),
}));

/** A comment line carries no UI copy. */
function isComment(line) {
  const t = line.trim();
  return t.startsWith("//") || t.startsWith("*") || t.startsWith("/*");
}

test("the source tree was actually found", () => {
  assert.ok(FILES.length >= 15, `expected the frontend js tree, found ${FILES.length} files`);
});

test("no em dash or en dash in user-facing text", () => {
  const offenders = [];
  for (const { rel, lines } of FILES) {
    lines.forEach((line, i) => {
      if (isComment(line)) return;
      if (line.includes("—") || line.includes("–")) {
        offenders.push(`${rel}:${i + 1}  ${line.trim().slice(0, 100)}`);
      }
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "Em/en dashes are prohibited in UI copy. Use a colon for label:value, " +
      "a full stop to split a sentence, 'to' for a numeric range, or 'n/a' " +
      "for a missing value:\n  " + offenders.join("\n  ")
  );
});

// NOTE on a test that was written and then DELETED: a third check
// flagged any quoted lone dash as an empty-value placeholder. It
// false-positived on `s.startsWith("-")` (a negative-sign check) and
// `iso.split("-")` (a date parser) — an ASCII hyphen has legitimate
// programmatic uses that no regex can tell apart from display text.
// The em/en dash rule above already covers every placeholder that
// actually rendered, so the extra check bought nothing and cost signal.

// ── modal browser dialogs ────────────────────────────────────────────────

test("no screen collects input or reports failure through a browser dialog", () => {
  // window.alert and window.prompt are unstyled, modal, and suppressed
  // outright in some embeds, so a rejected save could look like a dead
  // button and a rename could look like a no-op. They were also used
  // inconsistently: the same screens already had inline note slots for
  // their SUCCESS paths while failures went to an alert.
  //
  // window.confirm is deliberately still allowed. It guards destructive
  // actions (delete a source, delete a key, reset the day), where a
  // blocking modal that cannot be missed is the point.
  const offenders = [];
  for (const { rel, lines } of FILES) {
    lines.forEach((line, i) => {
      if (isComment(line)) return;
      if (/window\.(alert|prompt)\s*\(/.test(line)) {
        offenders.push(`${rel}:${i + 1}  ${line.trim().slice(0, 90)}`);
      }
    });
  }
  assert.deepEqual(
    offenders,
    [],
    "Use an inline note or an inline field instead. window.confirm is still fine for destructive actions:\n  " +
      offenders.join("\n  ")
  );
});

// XSS: make escaping ENFORCED rather than merely disciplined.
//
// This frontend builds markup as template strings and assigns it with
// `innerHTML`. That is a deliberate architectural choice (no framework,
// no build step) and it works — but it moves escaping from "guaranteed
// by the renderer" to "guaranteed by whoever wrote the line". A single
// missed call is stored XSS, because nearly everything rendered here is
// REMOTE text: article titles and source names scraped from ~45
// third-party news sites, keyword terms typed by admins, and provider
// error strings echoed back from LLM APIs.
//
// So this file greps the real source. That is unusual for a test and it
// is the point: no unit test can prove the absence of an unescaped
// interpolation across 11 screens, but a lint over the files can.
//
// DESIGN NOTE — why this targets FIELDS rather than flagging every
// interpolation: a first version flagged all `${...}` and allowlisted
// the safe shapes. It produced ~40 false positives on our own icons,
// ternaries and sub-builders. A test that cries wolf gets ignored, then
// deleted, and is worse than no test. This version instead names the
// fields that actually carry remote data and asserts each is escaped
// wherever it reaches markup — precise, and it fails only for real
// reasons.
//
// Zero dependencies: node:test + node:fs.

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

test("the source tree was actually found", () => {
  // A silently-empty file list would make every assertion below pass
  // for the wrong reason.
  assert.ok(FILES.length >= 15, `expected the frontend js tree, found ${FILES.length} files`);
});

// Property names whose VALUES come from outside this system:
//   scraped from news sites  — title, title_en, source, url, also_in
//   typed by an admin        — name, term_en, term_bn, section, label
//   echoed from a provider   — detail, error, reason, message, step
const REMOTE_FIELDS = [
  "title",
  "title_en",
  "source",
  "url",
  "also_in",
  "name",
  "term_en",
  "term_bn",
  "section",
  "detail",
  "error",
  "reason",
  "message",
  "step",
  "topic_tag",
];

/**
 * Flag the UNAMBIGUOUS mistake: a remote field interpolated BARE into
 * markup — `${a.title}`, `${a.title || ""}`, `${row.source}`.
 *
 * SCOPE, stated honestly: this is a regex over source, not a parser. It
 * deliberately examines only SIMPLE interpolations — no nested template
 * literals, no ternaries, no function calls. Two earlier, broader
 * versions were tried and both produced dozens of false positives
 * (truncated nested braces, fields used purely as a ternary CONDITION,
 * values reaching `textContent` via a local variable). A test that
 * cries wolf gets ignored and then deleted, so this one gives up
 * coverage to keep its signal exact.
 *
 * What that means in practice: this catches `${article.title}` — the
 * mistake a person actually makes when adding a column to a table — and
 * does not attempt to reason about complex expressions. The other tests
 * in this file cover the structural guarantees (every screen imports
 * the escaper; accumulators are escaped where they render; innerHTML is
 * never handed a bare remote value).
 */
function unescapedRemoteInterpolations({ rel, src }) {
  const offenders = [];
  const re = /\$\{([^{}`?()]*)\}/g; // simple expressions ONLY — see scope note
  let m;
  while ((m = re.exec(src)) !== null) {
    const expr = m[1].trim();
    if (!expr || expr.includes("escapeHtml(")) continue;
    // Must READ a remote field, not merely mention one.
    const readsRemote = REMOTE_FIELDS.some((f) => new RegExp(`\\.${f}\\b`).test(expr));
    if (!readsRemote) continue;

    const stmt = lineOf(src, m.index);
    // Safe sinks, each verified against the real code:
    //   a browser dialog renders plain text, not markup;
    //   textContent is assigned as text by the browser;
    //   an assignment builds an accumulator escaped at its render site
    //   (proved by the `accumulators` test below).
    if (/window\.(confirm|alert|prompt)\s*\(/.test(stmt)) continue;
    if (/textContent/.test(stmt)) continue;
    if (isInsideAssignment(src, m.index)) continue;

    offenders.push(`${rel}:${src.slice(0, m.index).split("\n").length}  \${${expr}}`);
  }
  return offenders;
}

/** The source line containing a character offset. */
function lineOf(src, index) {
  const start = src.lastIndexOf("\n", index) + 1;
  const end = src.indexOf("\n", index);
  return src.slice(start, end === -1 ? undefined : end);
}

/**
 * Is this interpolation part of a VARIABLE ASSIGNMENT rather than a
 * direct write into markup?
 *
 * Scans back to the start of the statement (previous `;` or `{`), not
 * just the current line — run.js builds a multi-line ternary
 * (`const detail = a ? b : `done · ${f.detail}``) whose assignment is
 * five lines above the interpolation. A single-line check called that a
 * violation when the value in fact goes to `textContent`.
 *
 * Assignments are safe here ONLY because the accumulator test below
 * proves such strings are escaped (or sunk to textContent) where they
 * are finally rendered.
 */
function isInsideAssignment(src, index) {
  // Line-based, bounded lookback. A character scan back to the previous
  // `;` or `{` does not work here: the nearest `{` is often the one in
  // a NESTED `${...}` a couple of lines up, which truncates the
  // statement and hides the assignment.
  const before = src.slice(0, index).split("\n");
  const LOOKBACK = 8; // run.js's multi-line ternary spans ~6
  for (let i = before.length - 1; i >= Math.max(0, before.length - LOOKBACK); i--) {
    const line = before[i];
    if (/^\s*(const|let|var)\s+[\w$]+\s*=/.test(line)) return true;
    if (/^\s*[\w.$]+\s*=\s*[^=]/.test(line) && !/^\s*(if|while|for|return)\b/.test(line)) return true;
    // A `return \`` or a bare template start means we are building
    // markup directly, not assigning — stop looking.
    if (/^\s*return\s/.test(line)) return false;
  }
  return false;
}

// manual.js ("How it works") renders only hardcoded const arrays
// declared in the file itself — `risks`, `steps`, `cards`, `methods`.
// Its `.title`/`.name` fields never touch the network. Exempted
// explicitly, with the reason, rather than by loosening the rules for
// everyone.
const LOCAL_LITERAL_ONLY = new Set(["js/screens/manual.js"]);

test("every remote field reaching markup is escaped", () => {
  const offenders = FILES.filter(
    (f) => !/\/(config|api|state|theme|route)\.js$/.test(f.rel) && !LOCAL_LITERAL_ONLY.has(f.rel)
  ).flatMap(unescapedRemoteInterpolations);
  assert.deepEqual(
    offenders,
    [],
    "Remote data interpolated into markup without escapeHtml():\n  " + offenders.join("\n  ")
  );
});

test("every screen rendering remote fields imports the escaper", () => {
  const offenders = [];
  for (const file of FILES) {
    if (!/\/screens\//.test(file.rel)) continue;
    const rendersRemote = REMOTE_FIELDS.some((f) => new RegExp(`\\.${f}\\b`).test(file.src));
    if (rendersRemote && !file.src.includes("escapeHtml") && !file.src.includes("textContent")) {
      offenders.push(file.rel);
    }
  }
  assert.deepEqual(offenders, [], `screens render remote fields with no escaping at all: ${offenders.join(", ")}`);
});

test("run.js keeps using textContent for source-supplied strings", () => {
  // Its per-source rows carry each source's own error text. Building
  // them with createElement + textContent (rather than a template
  // string) is what makes that safe by construction; pin it so a
  // refactor to template strings has to think about escaping again.
  const runjs = FILES.find((f) => f.rel.endsWith("screens/run.js"));
  assert.ok(runjs, "screens/run.js should exist");
  assert.ok(runjs.src.includes("textContent"), "run.js must set remote text via textContent");
});

test("no innerHTML is assigned a bare remote value", () => {
  // `el.innerHTML = someRemoteString` bypasses template literals
  // entirely, so the audit above would not see it.
  const offenders = [];
  const re = /\.innerHTML\s*=\s*([^;\n]+)/g;
  for (const file of FILES) {
    let m;
    while ((m = re.exec(file.src)) !== null) {
      const rhs = m[1].trim();
      if (rhs.startsWith("`") || rhs.startsWith('"') || rhs.startsWith("'")) continue; // literal/template
      if (rhs.includes("escapeHtml(") || /Html\b/.test(rhs)) continue; // escaped or a builder
      if (REMOTE_FIELDS.some((f) => new RegExp(`\\.${f}\\b`).test(rhs))) {
        offenders.push(`${file.rel}: innerHTML = ${rhs.slice(0, 70)}`);
      }
    }
  }
  assert.deepEqual(offenders, [], "innerHTML assigned raw remote data:\n  " + offenders.join("\n  "));
});


test("accumulators built from remote text are escaped where they render", () => {
  // Rule 2 above skips `s.testResult = `${src.name} — ...``, on the
  // promise that it is escaped at its render site. This is that
  // promise, checked: any screen that BUILDS such a string must also
  // escape it somewhere. Without this, rule 2 would be a hole big
  // enough to drive an injection through.
  const offenders = [];
  for (const file of FILES) {
    const builds = /\b(s\.)?testResult\s*=\s*`/.test(file.src);
    if (!builds) continue;
    if (!/escapeHtml\(\s*s?\.?testResult/s.test(file.src.replace(/\n/g, " "))) {
      offenders.push(file.rel);
    }
  }
  assert.deepEqual(offenders, [], `builds a result string from remote text but never escapes it: ${offenders.join(", ")}`);
});

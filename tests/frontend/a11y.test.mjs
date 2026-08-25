// Accessibility rules that can be checked from source.
//
// The app is a keyboard-and-screen-reader surface for a risk desk that
// people use all day. Two classes of defect were found by auditing the
// rendered pages, and both are the kind that come back the moment
// someone adds a control:
//
//   * A `type="date"` or `type="range"` input renders as a visible
//     widget but exposes NO accessible name. A screen reader announced
//     "slider" twice for the score range, with nothing to say which end
//     was which.
//   * A `placeholder` is not a label. It disappears the moment you type
//     and is not reliably announced.
//
// These are source-level checks, deliberately narrow, so they stay
// exact rather than crying wolf. The rendered-page audit that found
// them is not reproducible here without a DOM.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const JS_ROOT = new URL("../../frontend/js/", import.meta.url).pathname;
const CSS_PATH = new URL("../../frontend/css/app.css", import.meta.url).pathname;

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

/** Every `<input ...>` tag in a file, as whole strings. */
function inputTags(src) {
  return src.match(/<input\b[^>]*>/g) || [];
}

/** The ids some `<label for="...">` in this file actually points at. */
function labelledIds(src) {
  const ids = new Set();
  for (const m of src.matchAll(/<label\b[^>]*\bfor="([^"]*)"/g)) ids.add(m[1]);
  return ids;
}

/**
 * Does this `<input>` have an accessible name?
 *
 * THIS CHECK USED TO ACCEPT A BARE `id="..."` AS PROOF, which made the
 * two tests below a false guarantee rather than a guard. An id is not a
 * name. It is not announced, it is not displayed, and it connects to
 * nothing unless some `<label for>` points at it — and that was the
 * half nobody checked. Every input in this app carries an id, because
 * that is how each screen finds its own controls in `bind()`, so the
 * old predicate answered "named" for very nearly every tag handed to
 * it. Eleven genuinely unlabelled inputs passed a suite whose stated
 * purpose was catching exactly them, including the two boxes that take
 * an AI provider's API key and the three that change a password. The
 * suite was not merely missing them; it was standing in front of them
 * saying they had been checked.
 *
 * A name must now be real, and from source there are only two ways to
 * have one: an explicit `aria-label`/`aria-labelledby` on the tag, or
 * an id that a `<label for>` in the same file targets.
 *
 * KNOWN GAP, deliberately not covered: a `<label>` that WRAPS its input
 * also names it, and that cannot be seen without parsing HTML out of
 * template literals. Every wrapping label in this tree today is around
 * a checkbox, and checkboxes are excluded from the checks below, so
 * nothing is currently misreported. If a wrapped text input ever
 * appears, give it an `aria-label` rather than loosening this: an
 * explicit name is not a workaround, it is the better markup.
 */
function named(tag, labelled) {
  if (/aria-label=|aria-labelledby=/.test(tag)) return true;
  const id = (tag.match(/\bid="([^"]*)"/) || [])[1];
  return !!id && labelled.has(id);
}

/** Inputs that take typed or dragged input and so need a name of their own. */
function needsName(tag) {
  return !/type="(checkbox|radio|hidden|submit|button|reset|image|file)"/.test(tag);
}

/**
 * Unnamed inputs that are known, listed, and owned by someone else.
 *
 * `js/screens/export.js` is being edited concurrently in this same
 * pass, so its three controls (the top-N slider and the two date boxes
 * on the range picker) could not be fixed here without two people
 * writing one file. They are real defects and they are listed rather
 * than hidden: a slider and two date pickers with no name announce as
 * "slider", "edit text", "edit text", which on a date RANGE is exactly
 * as useless as it sounds, because the two ends are indistinguishable.
 *
 * THIS IS NOT A PLACE TO PARK AN INPUT YOU DID NOT FEEL LIKE
 * LABELLING. The last test in this file asserts that every entry is
 * STILL a genuine offender, so the moment export.js gains its labels
 * this suite goes red and names the line to delete. An allowlist that
 * cannot outlive the defect it excuses is a note. One that can is a
 * hole, and this file has already shipped one hole of that shape.
 */
const KNOWN_UNNAMED = new Map([["js/screens/export.js", ["topNRange", "rangeFrom", "rangeTo"]]]);

function excused(rel, tag) {
  const ids = KNOWN_UNNAMED.get(rel);
  if (!ids) return false;
  const id = (tag.match(/\bid="([^"]*)"/) || [])[1];
  return !!id && ids.includes(id);
}

test("date and range inputs carry an accessible name", () => {
  // Both render a visible widget with no implicit name of their own, so
  // without one they announce as a bare "edit text" or "slider".
  const offenders = [];
  for (const { rel, src } of FILES) {
    const labelled = labelledIds(src);
    for (const tag of inputTags(src)) {
      if (!/type="(date|range)"/.test(tag)) continue;
      if (named(tag, labelled) || excused(rel, tag)) continue;
      offenders.push(`${rel}  ${tag.slice(0, 90)}`);
    }
  }
  assert.deepEqual(offenders, [], "add aria-label (or an id with a matching <label for>):\n  " + offenders.join("\n  "));
});

test("a text input is not labelled by its placeholder alone", () => {
  // A placeholder vanishes as soon as the field has content, so it
  // cannot be the only thing naming the control.
  const offenders = [];
  for (const { rel, src } of FILES) {
    const labelled = labelledIds(src);
    for (const tag of inputTags(src)) {
      if (!/placeholder=/.test(tag)) continue;
      if (!needsName(tag)) continue;
      if (named(tag, labelled) || excused(rel, tag)) continue;
      offenders.push(`${rel}  ${tag.slice(0, 90)}`);
    }
  }
  assert.deepEqual(offenders, [], "placeholder is not a label:\n  " + offenders.join("\n  "));
});

test("every input a person types or drags in has an accessible name", () => {
  // The two checks above are each about one way of getting this wrong.
  // This one is the rule they are special cases of, and it is the check
  // that catches the fields with no visible hint at all: a password box
  // beside a `field-label` that never carried `for=`, or an API-key box
  // with nothing but a placeholder. There is no type of text input for
  // which "no name" is acceptable, so the general rule is worth stating
  // rather than enumerating types forever.
  //
  // Checkboxes, radios and buttons are excluded because they are named
  // by a wrapping <label> or their own text, neither of which is
  // visible to a source-level check. See `named`'s note.
  const offenders = [];
  for (const { rel, src } of FILES) {
    const labelled = labelledIds(src);
    for (const tag of inputTags(src)) {
      if (!needsName(tag)) continue;
      if (named(tag, labelled) || excused(rel, tag)) continue;
      offenders.push(`${rel}  ${tag.replace(/\s+/g, " ").slice(0, 100)}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "give each of these an aria-label, or an id with a matching <label for=\"...\">:\n  " + offenders.join("\n  ")
  );
});

test("the known-unnamed allowlist has no stale entries", () => {
  // An excuse that outlives the thing it excuses is how a lint quietly
  // stops meaning anything. If this fails, the fix has landed in the
  // file named below: delete that id from KNOWN_UNNAMED.
  const stale = [];
  for (const [rel, ids] of KNOWN_UNNAMED) {
    const file = FILES.find((f) => f.rel === rel);
    assert.ok(file, `KNOWN_UNNAMED names ${rel}, which no longer exists`);
    const labelled = labelledIds(file.src);
    for (const id of ids) {
      const tag = inputTags(file.src).find((t) => new RegExp(`\\bid="${id}"`).test(t));
      if (!tag) {
        stale.push(`${rel}: no input with id="${id}" any more`);
      } else if (named(tag, labelled)) {
        stale.push(`${rel}: id="${id}" is named now`);
      }
    }
  }
  assert.deepEqual(stale, [], "remove these from KNOWN_UNNAMED in this file:\n  " + stale.join("\n  "));
});

test("there is a global focus-visible style", () => {
  // The stylesheet had exactly one :focus-visible rule, on a single
  // control, so tabbing through the app moved an invisible cursor.
  const css = readFileSync(CSS_PATH, "utf8");
  assert.match(css, /^:focus-visible\s*\{/m, "a bare :focus-visible rule must exist so new controls inherit it");
  assert.match(css, /outline:/, ":focus-visible must actually draw an outline");
});

test("a skip link exists and is the app's first focusable element", () => {
  const shell = FILES.find((f) => f.rel === "js/shell.js");
  assert.ok(shell, "shell.js not found");
  assert.match(shell.src, /class="skip-link"/, "no skip link: reaching content means tabbing the whole rail");
  // It must precede the nav in source order, or it is not first in the
  // tab sequence and buys nothing.
  assert.ok(
    shell.src.indexOf('class="skip-link"') < shell.src.indexOf('id="railEl"'),
    "the skip link must come before the navigation rail in source order"
  );
});

test("the shell exposes landmarks", () => {
  const shell = FILES.find((f) => f.rel === "js/shell.js");
  assert.match(shell.src, /<aside[^>]*aria-label=/, "the rail needs a name to be a useful landmark");
  assert.match(shell.src, /<main\b/, "content must be in a <main> landmark");
});

test("icon-only controls in the shell are named", () => {
  // A `title` is a tooltip, not an accessible name on every platform.
  // These four are pure glyphs: gear, power, theme, rail toggle.
  const shell = FILES.find((f) => f.rel === "js/shell.js");
  for (const id of ["accountBtn", "signOutBtn", "topThemeBtn", "railToggleBtn"]) {
    const tag = (shell.src.match(new RegExp(`<button[^>]*id="${id}"[^>]*>`)) || [""])[0];
    assert.ok(tag, `${id} not found in shell.js`);
    assert.match(tag, /aria-label=/, `${id} renders as a bare glyph and needs an aria-label`);
  }
});

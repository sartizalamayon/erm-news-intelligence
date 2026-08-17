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

function named(tag) {
  return /aria-label=|aria-labelledby=|\bid="/.test(tag);
}

test("date and range inputs carry an accessible name", () => {
  // Both render a visible widget with no implicit name of their own, so
  // without one they announce as a bare "edit text" or "slider".
  const offenders = [];
  for (const { rel, src } of FILES) {
    for (const tag of inputTags(src)) {
      if (!/type="(date|range)"/.test(tag)) continue;
      if (named(tag)) continue;
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
    for (const tag of inputTags(src)) {
      if (!/placeholder=/.test(tag)) continue;
      if (/type="(checkbox|radio|hidden)"/.test(tag)) continue;
      if (named(tag)) continue;
      offenders.push(`${rel}  ${tag.slice(0, 90)}`);
    }
  }
  assert.deepEqual(offenders, [], "placeholder is not a label:\n  " + offenders.join("\n  "));
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

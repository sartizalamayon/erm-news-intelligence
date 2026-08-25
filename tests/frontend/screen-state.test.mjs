// Screen-local state, and the hooks that connect it to the DOM.
//
// These screens re-render by replacing `root.innerHTML` wholesale.
// That is a deliberate, framework-free choice and it works, but it has
// one rule attached: anything a person has put into the page and not
// yet submitted must live in the screen's state object, because the
// DOM it was typed into is about to be thrown away. Forget that rule
// and a re-render silently deletes work.
//
// The second half of this file is about the opposite direction: a
// binding that looks for something no template renders, or a rendered
// control that nothing binds. Both are invisible to every other gate.
// The app has shipped one of each — a `.js-wl-del` delete button with
// no handler for as long as it existed, and a `[data-toggle-body]`
// handler for a feature that had been replaced.

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

const byRel = (rel) => {
  const f = FILES.find((x) => x.rel === rel);
  assert.ok(f, `${rel} not found`);
  return f.src;
};

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

// ── a re-render must not eat what was typed ──────────────────────────

test("the keyword add form renders back what was typed into it", () => {
  // The three inputs rendered with no `value=` at all, so every
  // re-render handed back three empty boxes. Harmless in isolation, and
  // then `flash()` — which every validation and API failure on that
  // screen goes through — re-rendered on the spot. Fill in both terms,
  // forget to name the new section, press Add: the error telling you
  // what to fix arrived with both terms already deleted. One of those
  // terms is Bengali, so it is not a retype, it is a re-entry through
  // an IME.
  const src = byRel("js/screens/keywords.js");
  const form = bodyOf(src, src.indexOf("function keywordForm("));
  for (const id of ["kwFormEn", "kwFormBn", "kwFormNewSection"]) {
    const tag = (form.match(new RegExp(`<input[^>]*id="${id}"[^>]*>`)) || [""])[0];
    assert.ok(tag, `${id} not found in the add form`);
    assert.match(
      tag,
      /value="\$\{escapeHtml\(s\.form\./,
      `${id} must render from the draft in state, or a re-render deletes what was typed`
    );
  }
});

test("the keywords note timer does not re-render the screen", () => {
  // The 3.2-second expiry called render() a second time. Somebody who
  // read the error and started retyping inside that window had the DOM
  // rebuilt under them mid-keystroke: text gone, focus gone. The error
  // message told them what to do and the app then punished them for
  // doing it.
  //
  // Taking one node out is enough to clear a banner, and it cannot
  // disturb anything else on the page.
  const src = byRel("js/screens/keywords.js");
  const flash = bodyOf(src, src.indexOf("function flash("));
  const timer = flash.slice(flash.indexOf("setTimeout("));
  assert.ok(timer.length > 0, "flash() no longer uses a timer; re-check this test");
  assert.doesNotMatch(
    timer,
    /\brender\(\)/,
    "the note expiry must not re-render: it fires while the user may be typing"
  );
});

test("a keyword row offers a way to fix a typo", () => {
  // PATCH /keywords/{id} has existed since ADMIN-API and takes a
  // partial body, but the row only offered enable/disable and delete.
  // So correcting one character meant delete-and-re-add, which is not
  // the same operation: the row comes back with a new id, and
  // `import_keywords_xlsx` matches on id first, so the next
  // spreadsheet round-trip carrying the old id inserts a duplicate
  // instead of updating in place.
  const src = byRel("js/screens/keywords.js");
  const row = bodyOf(src, src.indexOf("function keywordRow("));
  assert.match(row, /data-edit="\$\{k\.id\}"/, "a keyword row needs an edit control");
  assert.match(src, /api\.patch\(`\/keywords\/\$\{[^}]+\}`/, "the edit control must save through PATCH /keywords/{id}");
});

// ── no hook without a partner ────────────────────────────────────────

const kebab = (s) => s.replace(/[A-Z]/g, (c) => "-" + c.toLowerCase());

test("no screen listens for a DOM hook that nothing renders", () => {
  // search.js bound `[data-toggle-body]` and wrote to an `openBodies`
  // Set. Nothing rendered that attribute and nothing read that Set:
  // both were left over from the inline-expand result list, which the
  // reading pane replaced. The binding therefore matched zero nodes on
  // every render, forever, and looked exactly like working code.
  //
  // Dead state next to live state is worse than dead code on its own,
  // because the next reader has to work out on their own that a screen
  // with a reading pane does not ALSO have a per-row expand model, and
  // nothing in the file tells them.
  const offenders = [];
  for (const { rel, src } of FILES) {
    const queried = new Set();
    for (const m of src.matchAll(/querySelector(?:All)?\(\s*[`"']\[(data-[a-z-]+)/g)) queried.add(m[1]);

    const rendered = new Set();
    // Written as an attribute in a template literal...
    for (const m of src.matchAll(/(?:^|\s)(data-[a-z-]+)(?=[\s=>"'`])/g)) rendered.add(m[1]);
    // ...or assigned through the dataset API, where it is camelCased.
    for (const m of src.matchAll(/\.dataset\.([A-Za-z0-9]+)\s*=/g)) rendered.add("data-" + kebab(m[1]));

    for (const hook of queried) {
      if (!rendered.has(hook)) offenders.push(`${rel}: binds [${hook}], which no template in that file renders`);
    }
  }
  assert.deepEqual(offenders, [], "dead hook:\n  " + offenders.join("\n  "));
});

test("every js- hook a screen renders is bound somewhere in that screen", () => {
  // The other direction, and the one that shipped a dead button. The
  // `.js-wl-del` remove control on a custom watchlist entity was
  // rendered with a data-id from the day self-service landed, and
  // nothing anywhere bound the class. Clicking it did nothing at all:
  // no removal, no error, not even a failed request in the network tab.
  //
  // `js-` prefixed classes exist for exactly one purpose, which is to
  // be queried, so an unqueried one is unambiguously a mistake. Ids and
  // plain data attributes are not included: an id can legitimately
  // exist only for a `<label for>` or an `aria-controls`.
  const offenders = [];
  for (const { rel, src } of FILES) {
    const rendered = new Set();
    for (const m of src.matchAll(/class="[^"]*?\b(js-[a-z-]+)/g)) rendered.add(m[1]);
    for (const hook of rendered) {
      if (!src.includes(`.${hook}`)) offenders.push(`${rel}: renders .${hook}, which nothing queries`);
    }
  }
  assert.deepEqual(offenders, [], "orphaned control:\n  " + offenders.join("\n  "));
});

// ── the account panel is a real dialog ───────────────────────────────

test("the account panel declares itself a modal dialog", () => {
  // It rendered as a plain div over a dimmed backdrop. Visually a
  // modal; to a screen reader, three password boxes appended to the end
  // of the page, with no announcement that anything had opened.
  const src = byRel("js/components/accountPanel.js");
  assert.match(src, /role="dialog"/, "no role=dialog: nothing tells a screen reader this is a dialog");
  assert.match(src, /aria-modal="true"/, "no aria-modal: the page behind is not marked inert to assistive tech");
  assert.match(src, /aria-labelledby="accountTitle"/, "a dialog needs a name, taken from its own heading");
  assert.match(src, /id="accountTitle"/, "aria-labelledby must point at something that exists");
});

test("the account panel can be closed and left with the keyboard", () => {
  // Escape did nothing. The only exits were the × and a click on the
  // backdrop, neither of which a keyboard user can reach without first
  // tabbing through the whole dialog — and Tab walked straight out of
  // the last control into the page underneath, which is invisible
  // behind a 55% black overlay. On a form whose fields are masked,
  // "focus is somewhere you cannot see" means typing a password into a
  // control you cannot identify.
  const src = byRel("js/components/accountPanel.js");
  assert.match(src, /e\.key === "Escape"/, "Escape must close the dialog");
  assert.match(src, /e\.key !== "Tab"/, "Tab must be trapped inside the dialog");
  assert.match(src, /#curPw"\)\?\.focus\(\)/, "opening the dialog must move focus into it");
  assert.match(src, /removeEventListener\("keydown"/, "the key handler must be torn down with the dialog");
  assert.match(src, /opener\.focus\(\)/, "closing must return focus to whatever opened the dialog");
});

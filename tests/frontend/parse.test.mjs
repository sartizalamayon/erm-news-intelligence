// Every module in the frontend actually parses, and every module that
// is meant to be importable outside a browser actually imports.
//
// This suite exists because a syntax error in a screen module could
// reach production without a single gate going red. Nothing imports the
// screens: route.test.mjs imports route.js, util.test.mjs imports
// util.js, and copy.test.mjs/xss.test.mjs read the tree as TEXT rather
// than loading it. There is no bundler and no type checker over this
// code, so this is the only thing standing between a stray character
// and a screen that will not load at all.
//
// The character that prompted it: an HTML comment written INSIDE a
// template literal, explaining an accessibility fix, which mentioned
// `for=` in backticks. A backtick inside a template literal ends the
// template literal. The rest of the function was then parsed as
// ordinary JavaScript, `for` was read as the keyword, and
// frontend/js/screens/review.js -- the app's primary triage screen --
// would not load at all. Every other gate stayed green: ruff and mypy
// do not read JavaScript, pytest does not import it, and the four
// frontend suites that existed never touch that file. A screen that
// does not parse is the most total failure this frontend has
// available, and it was the one thing nothing checked.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readdirSync, statSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";

const JS_ROOT = new URL("../../frontend/js/", import.meta.url).pathname;

// main.js is the browser entry point: it reads `document` at module
// scope, by design, because that is what an entry point is for. Every
// OTHER module in this tree is deliberately importable without a DOM --
// frontend/js/config.js carries an explicit no-browser branch and a
// comment explaining that this property is the single reason the
// frontend can be unit-tested at all. So main.js gets the syntax check
// alone, and the rest get both. If a second entry point is ever added,
// add it here with a note saying why, rather than weakening the check.
const DOM_ENTRY_POINTS = new Set(["js/main.js"]);

function jsFiles(dir = JS_ROOT, out = []) {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) jsFiles(full, out);
    else if (entry.endsWith(".js")) out.push(full);
  }
  return out;
}

const FILES = jsFiles().map((path) => ({ path, rel: path.split("/frontend/")[1] }));

test("the frontend source tree was actually found", () => {
  assert.ok(FILES.length >= 15, `expected the frontend js tree, found ${FILES.length} files`);
});

for (const { path, rel } of FILES) {
  test(`${rel} is valid ES module syntax`, () => {
    // Parsed by the same engine that will run it, as a module, with no
    // execution. Piped through stdin so no temporary file is needed and
    // the .js extension cannot be mistaken for CommonJS.
    try {
      execFileSync(process.execPath, ["--input-type=module", "--check"], {
        input: readFileSync(path),
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (err) {
      assert.fail(`${rel} is not valid ES module syntax:\n${String(err.stderr || err.message).trim()}`);
    }
  });
}

for (const { path, rel } of FILES.filter((f) => !DOM_ENTRY_POINTS.has(f.rel))) {
  test(`${rel} imports without a browser`, async () => {
    // Proves two more things the syntax check cannot: every path this
    // module imports from actually exists (a renamed or deleted module
    // is caught here), and nothing it does at module scope throws.
    try {
      await import(pathToFileURL(path).href);
    } catch (err) {
      assert.fail(`${rel} failed to load: ${(err && err.message) || err}`);
    }
  });
}

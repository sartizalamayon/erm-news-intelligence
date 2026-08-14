# Frontend tests

```bash
node --test "tests/frontend/*.test.mjs"
```

**No install step.** These use `node:test` and `node:assert`, both built
into Node (18+). That is deliberate: `frontend/` has no `package.json`,
no bundler and no `node_modules`, and adding a test framework would have
been the first crack in the property that makes this frontend cheap to
operate — nothing to install, nothing to upgrade, no supply chain.

Use `*.test.mjs`. Node's own discovery expects that suffix; a file named
`test_thing.mjs` is silently not collected.

## What is covered, and why these things

| File | Covers |
|---|---|
| `route.test.mjs` | URL ⇄ screen mapping, round-trips, admin gating, garbage input |
| `util.test.mjs` | `escapeHtml`, score tiers, percentage clamping, sparkline maths |
| `xss.test.mjs` | A lint over the real source: remote data must be escaped |

Three of these exist because of bugs that reached the running app:

* **The match rate was not a rate.** It divided a 14-day total by 14 and
  then by *today's* fetched count — an average-day numerator over a
  single-day denominator. Pure arithmetic, and no test would have let it
  ship.
* **Percentages fed CSS unclamped.** `pctOfMax` could return `"9990%"`,
  which is not a wrong number so much as a bar escaping its track.
* **Nothing had an address.** Navigation lived in memory, so no screen
  could be linked, Back did nothing, and refresh sent you to the
  Dashboard from wherever you were.

## About `xss.test.mjs`

It greps the source, which is unusual for a test. The reason: this app
builds markup as template strings and assigns it with `innerHTML`, so
escaping is guaranteed by *whoever wrote the line* rather than by a
renderer. No unit test can prove the absence of an unescaped
interpolation across eleven screens — a lint over the files can.

Its scope is deliberately narrow, and that was learned the hard way. A
first version flagged every `${...}` and allowlisted safe shapes: ~40
false positives on our own icons and ternaries. A second still tripped
on nested braces and fields used only as a ternary condition. **A test
that cries wolf gets ignored, then deleted, and is worse than no test.**

So it now flags only the unambiguous mistake — a remote field
interpolated bare, `${article.title}` — and leaves complex expressions
alone. That is the mistake a person actually makes when adding a column
to a table. The structural guarantees are asserted separately: every
screen that renders remote fields imports the escaper, `innerHTML` is
never handed a bare remote value, and any string built from remote text
is escaped where it finally renders.

**Verified to work**: planting `return \`<td>${a.title}</td>\`` in
`review.js` makes it fail with that exact line. If you change the audit,
re-run that check — a security test nobody has seen fail is just a
comment.

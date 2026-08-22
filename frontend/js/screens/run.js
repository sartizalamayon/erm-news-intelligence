// Run collection — the admin-only control surface for the whole
// fetch -> match -> enrich pipeline.
//
// Why this is its own screen (it used to be a strip on the Dashboard):
//   * It is admin-only and global. Nothing about it is per-user or
//     per-viewer, and it was sharing a screen whose entire purpose is
//     per-day reading. An analyst without admin rights saw a Dashboard
//     with a hole in it.
//   * It needs room. The per-source fetch list was capped at the last
//     12 entries to avoid growing the Dashboard card unbounded, which
//     meant the one view that tells you WHICH of ~45 sources failed
//     was the one thing you could not fully see.
//   * Its lifecycle is nothing like a dashboard's. A run outlives the
//     screen, the tab, and the session.
//
// ONE SURFACE, NOT TWO — the shape this screen has now:
// It used to carry two separate controls for the same five stages: a
// "Start a run" card with an all-or-nothing Enrich checkbox, and a
// second card below called "Re-run one AI stage" listing those same
// five stages as individual buttons. Same stages, two places, two code
// paths, two different behaviours (one backgrounded, one blocking the
// request until the stage finished). They are now one list: every step
// of the pipeline is a row, each row has a checkbox and its own "Run
// this", and both go through the SAME backgrounded endpoint
// (POST /health/pipeline/run, which now accepts a stage subset). The
// per-stage synchronous endpoint still exists server-side for API
// compatibility; this screen no longer calls it.
//
// Each row also shows WHICH PROVIDER AND MODEL the stage resolves to,
// live, and its LLM call accounting. Both were previously invisible or
// buried inside a prose `detail` string, which is how a stage whose
// every call failed could sit on screen reporting "done · processed 578
// article(s)".
//
// RENDERING CONTRACT — read before editing:
// This screen builds its rows ONCE and then PATCHES them. It must never
// re-render by reassigning innerHTML on a poll tick. The original
// implementation did exactly that (dashboard.js's `rerenderCurrent()`
// on a 500ms timer), which destroyed and rebuilt every node including
// the scrollable source list — so the list snapped back to the top
// roughly twice a second and reading it while a run was in progress was
// impossible. Patching in place is what fixes that; it is a correctness
// requirement of this screen, not an optimisation. `ensureStepRows()`
// below is the only thing that may build rows, it is guarded by a
// signature, and it is not reachable from the poll path.
//
// POLLING CONTRACT — this screen does NOT poll.
// It subscribes to `state.runStatus`, which shell.js's single app-wide
// poller publishes. An earlier version of this screen ran its own
// 1.5s interval against the same endpoint; combined with the shell's
// own 2s interval that was ~70 requests/minute of polling against a
// 120 req/min whole-API limit, and normal navigation on top pushed it
// over — the API returned 429 and the UI showed "Too many requests.
// Slow down and try again shortly." over a run that was perfectly
// healthy. Never reintroduce a second poller here. The one non-poll
// refresh below (`refreshStagePanel` when a run stops) is edge
// triggered: it fires once on the running -> finished transition, not
// on a timer.

import { api } from "../api.js";
import { pollRunStatus } from "../shell.js";
import { setState, state, subscribe } from "../state.js";
import { escapeHtml } from "../util.js";

const STAGE_PREFIX = "stage:";

// Status vocabulary, normalised. The API uses several spellings for the
// same three outcomes across phases ("ok"/"done", "error"/"failed") and
// one for "never started today" ("not_run"), so every renderer below
// goes through this rather than each growing its own ternary.
const TONE = {
  done: "ok",
  ok: "ok",
  error: "bad",
  failed: "bad",
  running: "live",
  pending: "wait",
  not_run: "idle",
};

const STATUS_LABEL = {
  done: "done",
  ok: "done",
  error: "failed",
  failed: "failed",
  running: "running",
  pending: "queued",
  not_run: "not run",
};

const MARK = { ok: "&#10003;", bad: "&#10005;", live: "&#9679;", wait: "&#8943;", idle: "&#9675;" };

function toneOf(status) {
  return TONE[status] || "idle";
}
function labelOf(status) {
  return STATUS_LABEL[status] || String(status || "not run").replace(/_/g, " ");
}

function fmtErr(err) {
  if (!err) return "Something went wrong";
  return err.message || String(err);
}

function num(n) {
  return Number(n || 0).toLocaleString();
}

// The one place that decides how call accounting reads. `calls === null`
// is NOT "nothing failed": it means this stage does not report per-call
// counts, which is a real state today (only the translate stage records
// them). Rendering that as a zeroed all-clear would recreate exactly the
// bug the accounting exists to expose, so it renders as "n/a" and says
// why on hover.
function callsBadge(calls, usesLlm) {
  if (usesLlm === false) {
    return { tone: "idle", text: "no LLM call", title: "This step calls no model, so there is nothing to count." };
  }
  if (!calls) {
    return {
      tone: "idle",
      text: "calls n/a",
      title: "This stage does not report per-call LLM counts yet, so no success or failure count can be shown.",
    };
  }
  const attempted = Number(calls.attempted || 0);
  const failed = Number(calls.failed || 0);
  const ok = Number(calls.succeeded || 0);
  if (attempted === 0) return { tone: "idle", text: "no calls made", title: "The stage made no LLM calls." };
  if (failed === 0) return { tone: "ok", text: `${num(ok)}/${num(attempted)} calls ok`, title: "" };
  if (ok === 0) {
    return {
      tone: "bad",
      text: `all ${num(attempted)} calls failed`,
      title: "Every LLM call this stage made failed. Nothing it wrote can be trusted.",
    };
  }
  return { tone: "warn", text: `${num(failed)} of ${num(attempted)} calls failed`, title: "" };
}

// "which provider and model", or an honest n/a with the resolver's own
// reason. Never guesses from the routing preference: a feature routed to
// a provider with no usable key silently resolves to the secondary, and
// a display built from the stored preference would name the wrong one.
//
// The label is the HEAD of the failover chain, because that is who
// answers unless something goes wrong. The full chain (ten candidates
// deep when keys are pooled) stays in the tooltip rather than being
// dropped or crammed into the row.
function routeBadge(step) {
  // One chip, not two, for a step with no model behind it: the calls
  // chip already says "no LLM call" and a second chip repeating it just
  // adds noise to the row.
  if (step.uses_llm === false) return null;
  if (step.route || step.route_primary) {
    const head = step.route_primary || step.route;
    const extra = Number(step.route_fallbacks || 0);
    return {
      tone: "route",
      text: extra > 0 ? `${head}  +${extra}` : head,
      title:
        `Feature: ${step.feature || "n/a"}\n` +
        (extra > 0 ? `Failover order:\n${String(step.route || head).split(" -> ").join("\n")}` : ""),
    };
  }
  if (step.route_error) return { tone: "bad", text: "no provider", title: step.route_error };
  return { tone: "idle", text: "provider n/a", title: "Not resolved yet." };
}

const CSS = `
.rp-head { display:flex; align-items:flex-start; justify-content:space-between; gap:16px; flex-wrap:wrap; }
.rp-title { font-size:16px; font-weight:600; letter-spacing:-.4px; margin:0; }
.rp-kicker { font-family:'JetBrains Mono', monospace; font-size:9.5px; letter-spacing:1.2px;
  text-transform:uppercase; color:var(--tx4); margin-top:3px; }
.rp-headline { display:flex; align-items:center; gap:10px; }
.rp-toolbar { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:16px 0 14px; }
.rp-toolbar .rp-spacer { flex:1; }
.rp-selnote { font-size:11.5px; color:var(--tx3); }
.rp-wrap { border:1px solid var(--line); border-radius:13px; overflow:hidden; background:var(--panel2); }
.rp-ghead { display:flex; align-items:center; gap:10px; padding:8px 14px; background:var(--raise);
  border-bottom:1px solid var(--line); }
.rp-ghead .rp-pick { padding-top:0; display:flex; align-items:center; }
.rp-ghead + .rp-ghead, .rp-steps + .rp-ghead { border-top:1px solid var(--line); }
.rp-gnum { font-family:'JetBrains Mono', monospace; font-size:9px; letter-spacing:1.4px; color:var(--tx4); }
.rp-gname { font-size:11.5px; font-weight:600; letter-spacing:.2px; text-transform:uppercase; color:var(--tx2); }
.rp-gnote { font-size:11px; color:var(--tx4); }
.rp-row { display:flex; align-items:flex-start; gap:12px; padding:13px 14px;
  border-bottom:1px solid var(--gridline); transition:background .18s ease, opacity .18s ease; }
.rp-steps > .rp-row:last-child { border-bottom:0; }
.rp-row[data-picked="0"] { opacity:.5; }
.rp-row[data-tone="live"] { background:var(--acsoft); }
.rp-row[data-tone="bad"] { background:var(--redsoft); }
.rp-pick { flex:0 0 16px; padding-top:3px; }
.rp-pick input { width:14px; height:14px; accent-color:var(--ac2); cursor:pointer; }
.rp-mark { flex:0 0 24px; height:24px; border-radius:50%; display:grid; place-items:center; font-size:11px;
  border:1px solid var(--line2); color:var(--tx4); background:var(--panel); }
.rp-row[data-tone="ok"] .rp-mark { border-color:var(--green); color:var(--green); background:var(--greensoft); }
.rp-row[data-tone="bad"] .rp-mark { border-color:var(--red); color:var(--red); background:var(--redsoft); }
.rp-row[data-tone="live"] .rp-mark { border-color:var(--ac2); color:var(--actx); background:var(--acsoft);
  animation:rpPulse 1.3s ease-in-out infinite; }
@keyframes rpPulse { 0%,100% { opacity:1; } 50% { opacity:.42; } }
.rp-body { flex:1; min-width:0; }
.rp-line1 { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.rp-name { font-size:13px; font-weight:600; letter-spacing:-.2px; }
.rp-desc { font-size:11.5px; color:var(--tx3); line-height:1.45; margin-top:2px; }
.rp-detail { font-size:11.5px; color:var(--tx3); line-height:1.5; margin-top:5px; word-break:break-word; }
.rp-row[data-tone="bad"] .rp-detail { color:var(--red); }
.rp-chips { display:flex; align-items:center; gap:6px; flex-wrap:wrap; margin-top:7px; }
.rp-chip { display:inline-flex; align-items:center; gap:5px; font-family:'JetBrains Mono', monospace;
  font-size:9.5px; letter-spacing:.4px; padding:3px 8px; border-radius:999px;
  border:1px solid var(--line); background:var(--panel); color:var(--tx3); max-width:100%;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rp-chip[data-tone="ok"] { border-color:transparent; background:var(--greensoft); color:var(--green); }
.rp-chip[data-tone="bad"] { border-color:transparent; background:var(--redsoft); color:var(--red); }
.rp-chip[data-tone="warn"] { border-color:transparent; background:var(--ambersoft); color:var(--amber); }
.rp-chip[data-tone="live"] { border-color:var(--acline); background:var(--acsoft); color:var(--actx); }
.rp-chip[data-tone="route"] { border-color:var(--acline); background:var(--acsoft); color:var(--actx);
  text-transform:none; letter-spacing:.2px; }
.rp-status { font-family:'JetBrains Mono', monospace; font-size:9px; letter-spacing:.9px; text-transform:uppercase;
  padding:2px 7px; border-radius:999px; background:var(--gridline); color:var(--tx3); }
.rp-status[data-tone="ok"] { background:var(--greensoft); color:var(--green); }
.rp-status[data-tone="bad"] { background:var(--redsoft); color:var(--red); }
.rp-status[data-tone="live"] { background:var(--acsoft); color:var(--actx); }
.rp-status[data-tone="warn"] { background:var(--ambersoft); color:var(--amber); }
.rp-bar { height:3px; border-radius:2px; background:var(--line); overflow:hidden; margin-top:8px; }
.rp-bar > div { height:100%; width:0%; background:var(--ac2); transition:width .4s ease; }
.rp-act { flex:0 0 auto; padding-top:1px; }
.rp-srcs { max-height:250px; overflow-y:auto; margin-top:9px; padding-left:11px;
  border-left:1px solid var(--line); }
.rp-src { display:flex; align-items:center; gap:8px; padding:2px 0; }
.rp-src-ic { width:12px; text-align:center; font-size:10px; }
.rp-src-nm { flex:0 0 auto; width:180px; font-size:11px; color:var(--tx2);
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rp-src-dt { font-size:10.5px; color:var(--tx4); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.rp-foot { font-size:11.5px; color:var(--tx4); line-height:1.55; margin:14px 0 0; }
`;

export function mountRun(root) {
  let alive = true;
  let unsubscribe = null;
  let elapsedTimer = null;
  // Identity of the run currently drawn, so a NEW run clears the source
  // list instead of appending onto the previous run's.
  let drawnRunKey = null;
  let sourceRowCount = 0;
  let startedAtMs = null;
  let finishedAtMs = null;
  let busy = false;
  let wasRunning = false;
  let rowSignature = "";
  // Idle-state rows, keyed by step key. Whatever the last completed run
  // of that stage recorded, plus the provider it would use now.
  let idleSteps = new Map();
  // Every step, once the row list is known. The old screen defaulted to
  // all three phases ticked; this keeps that default while making the
  // selection granular.
  let pickedInitialised = false;
  const picked = new Set();

  root.innerHTML = `<div class="page page-md">
    <style>${CSS}</style>

    <div class="card" style="margin-bottom:16px;">
      <div class="rp-head">
        <div>
          <div class="rp-headline">
            <h3 class="rp-title">Pipeline</h3>
            <span id="rpRunState" class="rp-status">idle</span>
          </div>
          <div class="rp-kicker">fetch &#8594; match &#8594; enrich</div>
        </div>
        <div style="text-align:right;">
          <div id="rpElapsed" class="mono" style="font-size:11px;color:var(--tx3);"></div>
          <p id="rpHint" class="hint" style="margin:2px 0 0;max-width:430px;"></p>
        </div>
      </div>

      <div class="rp-toolbar">
        <button id="rpRunBtn" class="btn btn-soft" style="white-space:nowrap;">&#9654; Run selected</button>
        <span id="rpSelNote" class="rp-selnote"></span>
        <span class="rp-spacer"></span>
        <button id="rpAll" class="btn-sm">Select all</button>
        <button id="rpNone" class="btn-sm">Select none</button>
      </div>

      <div class="rp-wrap" id="rpWrap"></div>

      <p class="rp-foot">
        Every step reads whatever the previous one left in the database, so any of them can be run on its own.
        A run continues on the server: you can leave this page, close the tab, or sign out.
      </p>
    </div>

    <div class="card">
      <h3 style="margin:0 0 4px;">Reset today</h3>
      <p class="hint" style="margin:0 0 12px;">
        Deletes today's collected articles <em>and</em> forgets their URLs, so the next run can fetch them
        again. Approved and rejected articles are always kept. Deleting rows without forgetting the URLs is
        what makes every later run report &ldquo;0 new&rdquo;.
      </p>
      <div class="row" style="gap:12px;">
        <button id="rpResetBtn" class="btn btn-outline" style="white-space:nowrap;">Reset today</button>
        <span id="rpResetNote" class="rp-selnote"></span>
      </div>
    </div>
  </div>`;

  const wrap = root.querySelector("#rpWrap");
  const runBtn = root.querySelector("#rpRunBtn");
  const resetBtn = root.querySelector("#rpResetBtn");
  const resetNote = root.querySelector("#rpResetNote");
  const runStateEl = root.querySelector("#rpRunState");
  const elapsedEl = root.querySelector("#rpElapsed");
  const hintEl = root.querySelector("#rpHint");
  const selNote = root.querySelector("#rpSelNote");

  runBtn.addEventListener("click", () => startRun([...picked]));
  resetBtn.addEventListener("click", resetToday);
  root.querySelector("#rpAll").addEventListener("click", () => setAllPicked(true));
  root.querySelector("#rpNone").addEventListener("click", () => setAllPicked(false));

  // ── row construction (ONCE, guarded) ─────────────────────────────────
  //
  // `stages` is the server's own stage list (GET /health/pipeline/stages,
  // or the enrich phase of a live run when that call has not landed yet).
  // The signature guard is what keeps this off the poll path: an
  // identical list rebuilds nothing, so a tick can never destroy the
  // scrolled source list underneath the fetch row.

  function ensureStepRows(stages) {
    const signature = stages.map((s) => s.key).join(",");
    if (signature === rowSignature) return;
    rowSignature = signature;
    sourceRowCount = 0;
    const stageKeys = stages.map((s) => STAGE_PREFIX + s.key);
    wrap.innerHTML = "";
    wrap.appendChild(groupHead("01", "Fetch", "pull today's articles from every enabled source", ["fetch"]));
    wrap.appendChild(stepsBlock([{ key: "fetch", name: "Fetch news", desc: "Every enabled source, in parallel" }]));
    wrap.appendChild(groupHead("02", "Match", "apply the keyword rules to what was fetched", ["match"]));
    wrap.appendChild(
      stepsBlock([{ key: "match", name: "Match keywords", desc: "Bilingual keyword rules, no model call" }])
    );
    // The group checkbox IS the old all-or-nothing "Enrich" tick box.
    // It now sits directly above the stages it controls instead of in a
    // different card from them, which is the whole point of this screen.
    wrap.appendChild(
      groupHead("03", "Enrich", `${stages.length} stages, run in dependency order`, stageKeys)
    );
    wrap.appendChild(stepsBlock(stages.map((s) => ({ ...s, key: STAGE_PREFIX + s.key }))));

    if (!pickedInitialised) {
      pickedInitialised = true;
      stepKeys().forEach((k) => picked.add(k));
    }

    wrap.querySelectorAll("[data-step]").forEach((cb) => {
      cb.checked = picked.has(cb.getAttribute("data-step"));
      cb.addEventListener("change", () => {
        const key = cb.getAttribute("data-step");
        if (cb.checked) picked.add(key);
        else picked.delete(key);
        syncPicked();
      });
    });
    wrap.querySelectorAll("[data-group-pick]").forEach((cb) => {
      cb.addEventListener("change", () => {
        const keys = (cb.getAttribute("data-group-pick") || "").split("|").filter(Boolean);
        keys.forEach((k) => (cb.checked ? picked.add(k) : picked.delete(k)));
        syncPicked();
      });
    });
    wrap.querySelectorAll("[data-run-only]").forEach((b) =>
      b.addEventListener("click", () => startRun([b.getAttribute("data-run-only")]))
    );
    syncPicked();
    renderIdleSteps();
  }

  function groupHead(numLabel, name, note, keys) {
    const el = document.createElement("div");
    el.className = "rp-ghead";
    el.dataset.ghead = name;
    el.innerHTML = `<label class="rp-pick">
        <input type="checkbox" data-group-pick="${escapeHtml(keys.join("|"))}" />
      </label>
      <span class="rp-gnum">${escapeHtml(numLabel)}</span>
      <span class="rp-gname">${escapeHtml(name)}</span>
      <span class="rp-gnote">${escapeHtml(note)}</span>`;
    return el;
  }

  function stepsBlock(steps) {
    const block = document.createElement("div");
    block.className = "rp-steps";
    steps.forEach((s) => block.appendChild(makeRow(s)));
    return block;
  }

  function makeRow(step) {
    const row = document.createElement("div");
    row.className = "rp-row";
    row.dataset.rowKey = step.key;
    row.dataset.tone = "idle";
    row.dataset.picked = "1";
    row.innerHTML = `
      <label class="rp-pick"><input type="checkbox" data-step="${escapeHtml(step.key)}" /></label>
      <div class="rp-mark" data-el="mark">${MARK.idle}</div>
      <div class="rp-body">
        <div class="rp-line1">
          <span class="rp-name">${escapeHtml(step.name || step.key)}</span>
          <span class="rp-status" data-el="status">not run</span>
        </div>
        <div class="rp-desc">${escapeHtml(step.desc || "")}</div>
        <div class="rp-chips">
          <span class="rp-chip" data-el="route"></span>
          <span class="rp-chip" data-el="calls"></span>
          <span class="rp-chip" data-el="when" style="display:none;"></span>
        </div>
        <div class="rp-detail" data-el="detail"></div>
        <div class="rp-bar" data-el="bar" style="display:none;"><div data-el="fill"></div></div>
      </div>
      <div class="rp-act"><button class="btn-sm" data-run-only="${escapeHtml(step.key)}">Run this</button></div>`;
    if (step.key === "fetch") {
      const list = document.createElement("div");
      list.className = "rp-srcs";
      list.dataset.el = "sources";
      list.style.display = "none";
      row.querySelector(".rp-body").appendChild(list);
    }
    return row;
  }

  // ── patching ─────────────────────────────────────────────────────────

  function rowFor(key) {
    return wrap.querySelector(`[data-row-key="${key}"]`);
  }

  function setChip(el, badge) {
    if (!badge || !badge.text) {
      el.style.display = "none";
      return;
    }
    el.style.display = "";
    el.dataset.tone = badge.tone;
    el.textContent = badge.text;
    el.title = badge.title || "";
  }

  /**
   * Patch ONE row. `step` carries whatever is known about it:
   * status/detail/route/calls plus optional live progress. Everything
   * that can contain remote text (a source's error, a provider's error
   * echoed back) is written with textContent, never innerHTML.
   */
  function patchRow(key, step) {
    const row = rowFor(key);
    if (!row) return;
    const tone = toneOf(step.status);
    row.dataset.tone = tone;
    row.querySelector('[data-el="mark"]').innerHTML = MARK[tone] || MARK.idle;
    const statusEl = row.querySelector('[data-el="status"]');
    statusEl.dataset.tone = tone;
    statusEl.textContent = step.in_run === false ? "not in this run" : labelOf(step.status);
    setChip(row.querySelector('[data-el="route"]'), routeBadge(step));
    setChip(row.querySelector('[data-el="calls"]'), callsBadge(step.calls, step.uses_llm));

    const when = row.querySelector('[data-el="when"]');
    if (step.ran_at) {
      when.style.display = "";
      when.dataset.tone = "";
      when.textContent = `last run ${shortTime(step.ran_at)}`;
      when.title = step.ran_at;
    } else {
      when.style.display = "none";
    }

    const det = row.querySelector('[data-el="detail"]');
    det.textContent = step.detail || "";
    det.title = step.detail || "";

    const bar = row.querySelector('[data-el="bar"]');
    const fill = bar.querySelector('[data-el="fill"]');
    if (step.status === "running" && Number(step.total) > 0) {
      bar.style.display = "";
      fill.style.width = `${Math.min(100, Math.round((step.done / step.total) * 100))}%`;
    } else {
      bar.style.display = "none";
    }
  }

  function shortTime(iso) {
    const t = Date.parse(iso);
    if (Number.isNaN(t)) return "n/a";
    return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // What a row shows when no run is in flight: today's recorded status
  // for the enrichment stages, and for fetch/match the last run this
  // server process actually performed (there is no persisted per-day
  // record for those two, and inventing one would be a lie).
  function renderIdleSteps() {
    if (!rowSignature) return;
    wrap.querySelectorAll("[data-row-key]").forEach((row) => {
      const key = row.dataset.rowKey;
      const known = idleSteps.get(key);
      patchRow(key, known || { status: "not_run", detail: "", uses_llm: key.startsWith(STAGE_PREFIX) });
    });
  }

  function syncPicked() {
    wrap.querySelectorAll("[data-row-key]").forEach((row) => {
      row.dataset.picked = picked.has(row.dataset.rowKey) ? "1" : "0";
    });
    wrap.querySelectorAll("[data-step]").forEach((cb) => {
      cb.checked = picked.has(cb.getAttribute("data-step"));
    });
    wrap.querySelectorAll("[data-group-pick]").forEach((cb) => {
      const keys = (cb.getAttribute("data-group-pick") || "").split("|").filter(Boolean);
      const on = keys.filter((k) => picked.has(k)).length;
      cb.checked = on > 0 && on === keys.length;
      cb.indeterminate = on > 0 && on < keys.length;
    });
    const n = picked.size;
    selNote.textContent = n === 0 ? "Nothing selected." : `${n} of ${stepKeys().length} steps selected.`;
    runBtn.disabled = busy || n === 0;
  }

  function stepKeys() {
    return [...wrap.querySelectorAll("[data-row-key]")].map((r) => r.dataset.rowKey);
  }

  function setAllPicked(on) {
    picked.clear();
    if (on) stepKeys().forEach((k) => picked.add(k));
    syncPicked();
  }

  function appendSourceRows(sources) {
    const row = rowFor("fetch");
    if (!row) return;
    const list = row.querySelector('[data-el="sources"]');
    if (!list) return;
    if (sources.length) list.style.display = "";
    for (let i = sourceRowCount; i < sources.length; i++) {
      const s = sources[i];
      const detail = s.status === "failed" ? s.error || "failed" : `${num(s.fetched)} fetched, ${num(s.inserted)} new`;
      const el = document.createElement("div");
      el.className = "rp-src";
      const ic = document.createElement("span");
      ic.className = "rp-src-ic";
      const tone = toneOf(s.status);
      ic.style.color = tone === "bad" ? "var(--red)" : tone === "ok" ? "var(--green)" : "var(--tx4)";
      ic.innerHTML = MARK[tone] || MARK.idle;
      const nm = document.createElement("span");
      nm.className = "rp-src-nm";
      nm.textContent = s.name;
      const dt = document.createElement("span");
      dt.className = "rp-src-dt";
      dt.textContent = detail;
      dt.title = detail;
      el.append(ic, nm, dt);
      list.appendChild(el);
    }
    sourceRowCount = sources.length;
  }

  // ── the live snapshot ────────────────────────────────────────────────

  function runKeyOf(poll) {
    return poll.started_at || null;
  }

  function fetchDetail(f) {
    if (f.status === "pending") return "Waiting to start.";
    if (f.status === "running") {
      const done = `${num(f.completed)} of ${f.total_sources || "?"} sources done`;
      return f.total_fetched ? `${done}, ${num(f.total_fetched)} fetched, ${num(f.total_inserted)} new` : done;
    }
    if (f.status === "error") return f.error || f.detail || "Fetch failed.";
    return f.detail || `${num(f.total_inserted)} new article(s)`;
  }

  function stageDetail(s) {
    if (s.status === "pending") return "Waiting to start.";
    if (s.status === "running") {
      const step = s.step || "running";
      if (Number(s.total) > 0) {
        const pct = Math.min(100, Math.round((s.done / s.total) * 100));
        return `${step}: ${num(s.done)} of ${num(s.total)} (${pct}%)`;
      }
      return `${step}...`;
    }
    return s.detail || "";
  }

  function applyPoll(poll) {
    const live = poll && poll.status && poll.status !== "idle";
    const running = Boolean(poll && poll.status === "running");
    setRunning(running);

    if (!live) {
      runStateEl.dataset.tone = "";
      runStateEl.textContent = "idle";
      hintEl.textContent = "No run has started since this server was last restarted.";
      elapsedEl.textContent = "";
      renderIdleSteps();
      return;
    }

    const key = runKeyOf(poll);
    if (key !== drawnRunKey) {
      drawnRunKey = key;
      startedAtMs = poll.started_at ? Date.parse(poll.started_at) : Date.now();
      finishedAtMs = null;
      // A new run: drop the PREVIOUS run's per-source rows. This clears
      // one child list; it does not rebuild the panel.
      const fetchRow = rowFor("fetch");
      const list = fetchRow && fetchRow.querySelector('[data-el="sources"]');
      if (list) {
        list.innerHTML = "";
        list.style.display = "none";
      }
      sourceRowCount = 0;
    }
    finishedAtMs = running ? null : poll.finished_at ? Date.parse(poll.finished_at) : finishedAtMs || Date.now();

    const stages = (poll.enrich && poll.enrich.stages) || [];
    const seen = new Set();

    if (poll.fetch) {
      seen.add("fetch");
      patchRow("fetch", {
        status: poll.fetch.status,
        detail: fetchDetail(poll.fetch),
        done: poll.fetch.completed,
        total: poll.fetch.total_sources,
        uses_llm: false,
      });
      appendSourceRows(poll.fetch.sources || []);
    }
    if (poll.match) {
      seen.add("match");
      patchRow("match", {
        status: poll.match.status,
        detail: poll.match.status === "pending" ? "Waiting to start." : poll.match.detail || "",
        uses_llm: false,
      });
    }
    stages.forEach((s) => {
      seen.add(STAGE_PREFIX + s.key);
      patchRow(STAGE_PREFIX + s.key, {
        status: s.status,
        detail: stageDetail(s),
        done: s.done,
        total: s.total,
        route: s.route,
        route_primary: s.route_primary,
        route_fallbacks: s.route_fallbacks,
        route_error: s.route_error,
        feature: s.feature,
        uses_llm: s.uses_llm,
        calls: s.calls,
      });
    });

    // Steps this run did not include keep their idle information rather
    // than being blanked, and say so.
    stepKeys().forEach((k) => {
      if (seen.has(k)) return;
      const known = idleSteps.get(k) || { status: "not_run", uses_llm: k.startsWith(STAGE_PREFIX) };
      patchRow(k, { ...known, in_run: false });
    });

    const hasError =
      poll.status === "error" ||
      (poll.fetch && poll.fetch.status === "error") ||
      (poll.match && poll.match.status === "error") ||
      stages.some((s) => s.status === "error");

    runStateEl.dataset.tone = running ? "live" : hasError ? "bad" : "ok";
    runStateEl.textContent = running ? "running" : hasError ? "finished with errors" : "finished";
    hintEl.textContent = running
      ? "Running on the server. Leaving this page will not stop it."
      : hasError
      ? "One or more steps failed. Their rows are marked below."
      : "Last run finished cleanly.";
    tickElapsed();
  }

  function setRunning(running) {
    if (busy === running) {
      // Still keep the run button in step with the selection.
      runBtn.disabled = busy || picked.size === 0;
      return;
    }
    busy = running;
    runBtn.innerHTML = running ? "&#9654; Running..." : "&#9654; Run selected";
    runBtn.disabled = running || picked.size === 0;
    resetBtn.disabled = running;
    wrap.querySelectorAll("[data-step], [data-group-pick]").forEach((cb) => {
      cb.disabled = running;
    });
    // Starting a second run against articles the first is concurrently
    // rewriting is a race with no useful outcome, so every per-step
    // button follows the same lock.
    wrap.querySelectorAll("[data-run-only]").forEach((b) => {
      b.disabled = running;
    });
  }

  function tickElapsed() {
    if (!startedAtMs) return;
    const end = finishedAtMs || Date.now();
    elapsedEl.textContent = `${Math.max(0, Math.round((end - startedAtMs) / 1000))}s elapsed`;
  }

  // ── data in ──────────────────────────────────────────────────────────

  async function refreshStagePanel() {
    try {
      const rows = await api.get("/health/pipeline/stages");
      if (!alive) return;
      ensureStepRows(rows);
      rows.forEach((r) => {
        idleSteps.set(STAGE_PREFIX + r.key, {
          status: r.status,
          detail: r.detail || "",
          ran_at: r.ran_at,
          route: r.route,
          route_primary: r.route_primary,
          route_fallbacks: r.route_fallbacks,
          route_error: r.route_error,
          feature: r.feature,
          uses_llm: r.uses_llm,
          calls: r.calls,
        });
      });
      // Never overwrite a live run's rows with idle data.
      if (!state.runStatus || state.runStatus.status === "idle" || state.runStatus.status !== "running") {
        renderFromState();
      }
    } catch (err) {
      if (!alive) return;
      hintEl.textContent = "Could not read the stage list: " + fmtErr(err);
      // Fall back to whatever a live run reports, so the page is never
      // blank just because this one call failed.
      const s = state.runStatus;
      if (s && s.enrich && s.enrich.stages && s.enrich.stages.length) ensureStepRows(s.enrich.stages);
    }
  }

  function renderFromState() {
    if (!alive) return;
    const poll = state.runStatus;
    // Rows may not exist yet on the very first publish. Build them from
    // the live payload rather than showing nothing.
    if (!rowSignature && poll && poll.enrich && (poll.enrich.stages || []).length) {
      ensureStepRows(poll.enrich.stages);
    }
    if (!rowSignature) return;
    applyPoll(poll);
    const nowRunning = Boolean(poll && poll.status === "running");
    if (wasRunning && !nowRunning) {
      // Edge triggered, once per run: pick up the freshly recorded
      // status, call counts and routes. NOT a poller.
      wasRunning = false;
      refreshStagePanel();
    }
    wasRunning = nowRunning;
  }

  // ── actions ──────────────────────────────────────────────────────────

  async function startRun(keys) {
    if (busy) return;
    const wanted = keys.filter((k) => stepKeys().includes(k));
    if (!wanted.length) {
      hintEl.textContent = "Pick at least one step to run.";
      return;
    }
    const stages = wanted.filter((k) => k.startsWith(STAGE_PREFIX)).map((k) => k.slice(STAGE_PREFIX.length));
    const body = {
      fetch: wanted.includes("fetch"),
      match: wanted.includes("match"),
      enrich: stages.length > 0,
      stages: stages.length ? stages : null,
    };
    setRunning(true);
    hintEl.textContent = "";
    try {
      const data = await api.post("/health/pipeline/run", body);
      // Publish the start snapshot through the same channel the poller
      // uses, so the header pill and these rows agree immediately
      // instead of waiting up to one poll interval.
      setState({ runStatus: data });
    } catch (err) {
      setRunning(false);
      hintEl.textContent = fmtErr(err);
      return;
    }
    // One immediate refresh for responsiveness; the shell's interval
    // takes it from here.
    pollRunStatus();
  }

  async function resetToday() {
    if (busy) return;
    const ok = window.confirm(
      "Reset today?\n\n" +
        "Deletes today's collected articles and forgets their URLs so they can be fetched again.\n" +
        "Approved and rejected articles are kept."
    );
    if (!ok) return;
    resetBtn.disabled = true;
    resetNote.textContent = "Resetting...";
    try {
      const res = await api.post("/health/pipeline/reset-day", { keep_reviewed: true });
      resetNote.textContent = `Reset ${res.day}: ${res.deleted} article(s) deleted, ${res.forgotten_urls} URL(s) forgotten.`;
      setState({ pendingCount: 0 });
    } catch (err) {
      resetNote.textContent = fmtErr(err);
    } finally {
      resetBtn.disabled = busy;
    }
  }

  // Draw immediately from the last published snapshot (so arriving on
  // this screen mid-run shows progress without waiting a tick), then
  // re-draw whenever the shell's poller publishes a new one.
  renderFromState();
  refreshStagePanel();
  unsubscribe = subscribe(renderFromState);
  // Nudge the shared poller once on mount so a stale snapshot isn't
  // shown for up to a full interval. This is one request, not a loop.
  pollRunStatus();

  // Elapsed time ticks locally so the number moves smoothly between
  // polls; it patches ONE text node, never the panel.
  elapsedTimer = setInterval(() => {
    if (alive && busy) tickElapsed();
  }, 1000);

  return {
    destroy() {
      alive = false;
      if (unsubscribe) unsubscribe();
      if (elapsedTimer) clearInterval(elapsedTimer);
      unsubscribe = null;
      elapsedTimer = null;
      idleSteps = new Map();
    },
  };
}

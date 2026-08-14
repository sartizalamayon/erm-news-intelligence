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
// RENDERING CONTRACT — read before editing:
// This screen builds its progress DOM ONCE and then PATCHES it. It must
// never re-render the panel by reassigning innerHTML on a poll tick.
// The previous implementation did exactly that (dashboard.js's
// `rerenderCurrent()` on a 500ms timer), which destroyed and rebuilt
// every node including the scrollable source list — so the list snapped
// back to the top roughly twice a second and reading it while a run was
// in progress was impossible. Patching in place is what fixes that; it
// is a correctness requirement of this screen, not an optimisation.
//
// POLLING CONTRACT — this screen does NOT poll.
// It subscribes to `state.runStatus`, which shell.js's single app-wide
// poller publishes. An earlier version of this screen ran its own
// 1.5s interval against the same endpoint; combined with the shell's
// own 2s interval that was ~70 requests/minute of polling against a
// 120 req/min whole-API limit, and normal navigation on top pushed it
// over — the API returned 429 and the UI showed "Too many requests.
// Slow down and try again shortly." over a run that was perfectly
// healthy. Never reintroduce a second poller here.

import { api } from "../api.js";
import { pollRunStatus } from "../shell.js";
import { setState, state, subscribe } from "../state.js";
import { escapeHtml } from "../util.js";

const STATUS_ICON = { done: "&#10003;", ok: "&#10003;", error: "&#10005;", failed: "&#10005;", running: "&#8987;" };
const STATUS_COLOR = {
  done: "var(--green)",
  ok: "var(--green)",
  error: "var(--red)",
  failed: "var(--red)",
  running: "var(--actx)",
};

function iconFor(status) {
  return STATUS_ICON[status] || "&#183;";
}
function colorFor(status) {
  return STATUS_COLOR[status] || "var(--tx4)";
}

function fmtErr(err) {
  if (!err) return "Something went wrong";
  return err.message || String(err);
}

export function mountRun(root) {
  let alive = true;
  let unsubscribe = null;
  let elapsedTimer = null;
  // Identity of the run currently drawn, so a NEW run resets the panel
  // instead of appending onto the previous run's source list.
  let drawnRunKey = null;
  let sourceRowCount = 0;
  let startedAtMs = null;
  let finishedAtMs = null;
  let busy = false;

  const phases = { fetch: true, match: true, enrich: true };

  root.innerHTML = `<div class="page">
    <div class="card" style="margin-bottom:16px;">
      <div class="row-between" style="margin-bottom:4px;">
        <h3 style="margin:0;">Start a run</h3>
        <span id="runIdleHint" class="hint" style="margin:0;"></span>
      </div>
      <p class="hint" style="margin:0 0 12px;">
        Fetch pulls today's articles from every enabled source. Match applies your keyword rules.
        Enrich runs the five AI stages. Pick any combination — each phase reads whatever the previous
        one left in the database, so you can re-run them independently.
      </p>
      <div class="row wrap" style="gap:12px;align-items:center;">
        <div class="row" style="gap:14px;">
          ${["fetch", "match", "enrich"]
            .map(
              (k) => `<label class="row" data-phase-label="${k}" style="gap:6px;align-items:center;font-size:12px;color:var(--tx2);cursor:pointer;">
                <input type="checkbox" data-phase="${k}" checked style="accent-color:var(--ac2);width:14px;height:14px;" />
                ${k[0].toUpperCase() + k.slice(1)}
              </label>`
            )
            .join("")}
        </div>
        <button id="runBtn" class="btn btn-soft" style="white-space:nowrap;">&#9654; Run collection</button>
        <div class="grow"></div>
        <button id="resetDayBtn" class="btn btn-outline" style="white-space:nowrap;">Reset today</button>
      </div>
      <p class="hint" id="resetHint" style="margin:10px 0 0;">
        Reset today deletes today's collected articles <em>and</em> forgets their URLs, so the next run
        can fetch them again. Approved and rejected articles are always kept. Deleting rows without
        forgetting the URLs is what makes every later run report &ldquo;0 new&rdquo;.
      </p>
    </div>

    <div id="runPanel" class="card" style="display:none;">
      <div class="row-between" style="margin-bottom:4px;">
        <h3 id="runPanelTitle" style="margin:0;">Run collection</h3>
        <span id="runElapsed" class="mono" style="font-size:11px;color:var(--tx3);"></span>
      </div>
      <p class="hint" id="runPanelHint" style="margin:0 0 10px;"></p>
      <div class="stack" style="gap:0;" id="runRows"></div>
    </div>
  </div>`;

  const runBtn = root.querySelector("#runBtn");
  const resetBtn = root.querySelector("#resetDayBtn");
  const panel = root.querySelector("#runPanel");
  const rowsEl = root.querySelector("#runRows");
  const titleEl = root.querySelector("#runPanelTitle");
  const elapsedEl = root.querySelector("#runElapsed");
  const hintEl = root.querySelector("#runPanelHint");
  const idleHint = root.querySelector("#runIdleHint");

  root.querySelectorAll("[data-phase]").forEach((cb) => {
    cb.addEventListener("change", () => {
      phases[cb.getAttribute("data-phase")] = cb.checked;
    });
  });
  runBtn.addEventListener("click", startRun);
  resetBtn.addEventListener("click", resetToday);

  // ── row construction (once per run) ──────────────────────────────────

  function makeRow(key, label) {
    const row = document.createElement("div");
    row.dataset.rowKey = key;
    row.innerHTML = `<div class="row" style="gap:11px;padding:6px 0;">
      <span data-el="icon" style="width:16px;text-align:center;font-size:13px;color:var(--tx4);">&#183;</span>
      <span style="flex:0 0 150px;font-size:12.5px;font-weight:600;">${escapeHtml(label)}</span>
      <span data-el="detail" style="flex:1;min-width:0;font-size:11.5px;color:var(--tx3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"></span>
    </div>`;
    return row;
  }

  // A running stage says WHAT it is doing and HOW FAR along it is.
  // "running…" alone made a slow stage and a hung stage look identical,
  // which is precisely how a genuinely stuck stage went unnoticed for
  // several minutes. Percentage is included because "1030/1752" is hard
  // to read at a glance while it moves.
  function stageDetailText(s) {
    if (s.status === "pending") return "waiting…";
    if (s.status === "error") return s.detail || "failed";
    if (s.status === "running") {
      const step = s.step || "running";
      if (s.total > 0) {
        const pct = Math.min(100, Math.round((s.done / s.total) * 100));
        return `${step} · ${s.done.toLocaleString()} / ${s.total.toLocaleString()} (${pct}%)`;
      }
      return `${step}…`;
    }
    return `${s.status}${s.detail ? " · " + s.detail : ""}`;
  }

  // Thin determinate progress bar under a running stage. Created on
  // demand and removed when the stage stops running, so a finished row
  // collapses back to a single line.
  function setStageBar(key, s) {
    const row = rowsEl.querySelector(`[data-row-key="${key}"]`);
    if (!row) return;
    const running = s.status === "running" && s.total > 0;
    let bar = row.querySelector('[data-el="bar"]');
    if (!running) {
      if (bar) bar.remove();
      return;
    }
    if (!bar) {
      bar = document.createElement("div");
      bar.dataset.el = "bar";
      bar.style.cssText =
        "height:3px;border-radius:2px;background:var(--line);margin:0 0 6px 27px;overflow:hidden;";
      const fill = document.createElement("div");
      fill.dataset.el = "fill";
      fill.style.cssText = "height:100%;width:0%;background:var(--ac2);transition:width .4s ease;";
      bar.appendChild(fill);
      row.appendChild(bar);
    }
    const fill = bar.querySelector('[data-el="fill"]');
    fill.style.width = `${Math.min(100, Math.round((s.done / s.total) * 100))}%`;
  }

  function setRow(key, status, detail) {
    const row = rowsEl.querySelector(`[data-row-key="${key}"]`);
    if (!row) return;
    const icon = row.querySelector('[data-el="icon"]');
    const det = row.querySelector('[data-el="detail"]');
    icon.innerHTML = iconFor(status);
    icon.style.color = colorFor(status);
    // textContent, not innerHTML — this string can contain a source's
    // own error text, which is remote input.
    det.textContent = detail || "";
    det.title = detail || "";
  }

  function buildPanel(poll) {
    rowsEl.innerHTML = "";
    sourceRowCount = 0;

    if (poll.fetch) {
      rowsEl.appendChild(makeRow("fetch", "Fetch news"));
      // The scrollable per-source list. Built once; only APPENDED to.
      // This element's scrollTop is exactly what the old full re-render
      // was destroying every tick.
      const list = document.createElement("div");
      list.id = "sourceList";
      list.style.cssText =
        "max-height:260px;overflow-y:auto;margin:2px 0 8px 27px;padding-left:9px;border-left:1px solid var(--line);";
      rowsEl.appendChild(list);
    }
    if (poll.match) rowsEl.appendChild(makeRow("match", "Match keywords"));
    if (poll.enrich) {
      (poll.enrich.stages || []).forEach((s) => rowsEl.appendChild(makeRow(`stage:${s.key}`, s.name)));
    }
  }

  function appendSourceRows(sources) {
    const list = rowsEl.querySelector("#sourceList");
    if (!list) return;
    for (let i = sourceRowCount; i < sources.length; i++) {
      const s = sources[i];
      const detail = s.status === "failed" ? s.error || "failed" : `${s.fetched} fetched · ${s.inserted} new`;
      const row = document.createElement("div");
      row.className = "row";
      row.style.cssText = "gap:8px;padding:2px 0;";
      const ic = document.createElement("span");
      ic.style.cssText = `width:12px;text-align:center;font-size:11px;color:${colorFor(s.status)};`;
      ic.innerHTML = iconFor(s.status);
      const nm = document.createElement("span");
      nm.style.cssText =
        "flex:0 0 auto;width:170px;font-size:11px;color:var(--tx2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      nm.textContent = s.name;
      const dt = document.createElement("span");
      dt.style.cssText =
        "font-size:10.5px;color:var(--tx4);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;";
      dt.textContent = detail;
      dt.title = detail;
      row.append(ic, nm, dt);
      list.appendChild(row);
    }
    sourceRowCount = sources.length;
  }

  // ── polling ──────────────────────────────────────────────────────────

  function runKeyOf(poll) {
    return poll.started_at || null;
  }

  function applyPoll(poll) {
    if (!poll || poll.status === "idle") {
      panel.style.display = "none";
      idleHint.textContent = "No run in progress.";
      setRunning(false);
      return;
    }
    idleHint.textContent = "";
    panel.style.display = "";

    const key = runKeyOf(poll);
    if (key !== drawnRunKey) {
      drawnRunKey = key;
      buildPanel(poll);
      startedAtMs = poll.started_at ? Date.parse(poll.started_at) : Date.now();
      finishedAtMs = null;
    }

    const running = poll.status === "running";
    setRunning(running);
    finishedAtMs = running ? null : poll.finished_at ? Date.parse(poll.finished_at) : finishedAtMs || Date.now();

    const hasError =
      poll.status === "error" ||
      (poll.fetch && poll.fetch.status === "error") ||
      (poll.match && poll.match.status === "error") ||
      (poll.enrich && (poll.enrich.stages || []).some((s) => s.status === "error"));

    titleEl.textContent = `Run collection · ${running ? "running" : hasError ? "finished with errors" : "done"}`;
    panel.style.borderColor = running ? "var(--ac2)" : hasError ? "var(--red)" : "var(--line)";
    hintEl.textContent = running
      ? "This run is executing on the server. You can leave this page, close the tab, or sign out — it will keep going."
      : "Finished. Re-run any phase above; each reads whatever the previous one left in the database.";

    if (poll.fetch) {
      const f = poll.fetch;
      const detail =
        f.status === "pending"
          ? "waiting…"
          : f.status === "running"
          ? `fetching · ${f.completed || 0}/${f.total_sources || "?"} sources done`
          : f.status === "error"
          ? f.error || f.detail || "fetch failed"
          : `done · ${f.detail || ""}`;
      setRow("fetch", f.status, detail);
      appendSourceRows(f.sources || []);
    }
    if (poll.match) {
      const m = poll.match;
      setRow(
        "match",
        m.status,
        m.status === "pending" ? "waiting…" : m.status === "running" ? "matching…" : m.detail || m.status
      );
    }
    if (poll.enrich) {
      (poll.enrich.stages || []).forEach((s) => {
        setRow(`stage:${s.key}`, s.status, stageDetailText(s));
        setStageBar(`stage:${s.key}`, s);
      });
    }
    tickElapsed();
  }

  function setRunning(running) {
    busy = running;
    runBtn.disabled = running;
    runBtn.innerHTML = running ? "&#9654; Running…" : "&#9654; Run collection";
    resetBtn.disabled = running;
    root.querySelectorAll("[data-phase]").forEach((cb) => {
      cb.disabled = running;
    });
  }

  function tickElapsed() {
    if (!startedAtMs) return;
    const end = finishedAtMs || Date.now();
    elapsedEl.textContent = `${Math.max(0, Math.round((end - startedAtMs) / 1000))}s elapsed`;
  }

  // Render from whatever the shell's poller last published. Called on
  // mount and on every state change — never issues a request itself.
  function renderFromState() {
    if (!alive) return;
    applyPoll(state.runStatus);
  }

  async function startRun() {
    if (busy) return;
    const selected = { ...phases };
    if (!selected.fetch && !selected.match && !selected.enrich) {
      idleHint.textContent = "Pick at least one phase.";
      return;
    }
    setRunning(true);
    idleHint.textContent = "";
    try {
      const data = await api.post("/health/pipeline/run", selected);
      // Publish the start snapshot through the same channel the poller
      // uses, so the header pill and this panel agree immediately
      // instead of waiting up to one poll interval.
      setState({ runStatus: data });
    } catch (err) {
      setRunning(false);
      idleHint.textContent = fmtErr(err);
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
    try {
      const res = await api.post("/health/pipeline/reset-day", { keep_reviewed: true });
      idleHint.textContent = `Reset ${res.day}: ${res.deleted} article(s) deleted, ${res.forgotten_urls} URL(s) forgotten.`;
      setState({ pendingCount: 0 });
    } catch (err) {
      idleHint.textContent = fmtErr(err);
    } finally {
      resetBtn.disabled = false;
    }
  }

  // Draw immediately from the last published snapshot (so arriving on
  // this screen mid-run shows progress without waiting a tick), then
  // re-draw whenever the shell's poller publishes a new one.
  renderFromState();
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
    },
  };
}

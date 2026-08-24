// AI prompts: the editorial half of phase 3.
//
// Everything here shapes WHAT the models are asked. Which vendor and
// model actually answers now lives on the AI models screen
// (frontend/js/screens/models.js): providers, pooled API keys, and
// per-feature routing all moved there.
//
// The two halves were one screen, and it did not work. They are edited
// by different people at different times for different reasons, so
// changing a model id meant scrolling past four editorial cards, and
// the routing controls sat directly below a candidate keyword list they
// have nothing to do with.
//
// LAYOUT: six editors, grouped by STAGE, in pipeline order.
//
// Scoring used to be the only stage with editable text, so two cards
// stacked in a column was the whole page. Six of those would be a wall:
// every card the same size, the same three buttons, nothing saying
// which runs first or which one you actually want. So the page is a
// numbered walk through the pipeline instead, each stage explaining
// itself in plain language, and every editor collapsed to a one-line
// summary until you open it. Collapsed, the whole pipeline fits on one
// screen and you can see at a glance which prompts have been edited
// away from the built-in.
//
// Real API (live):
//   GET  /prompts/config
//   PUT  /prompts/stages/{field}
//   GET  /prompts/stages/{field}/versions
//   POST /prompts/stages/{field}/versions/{v}/activate
//   GET  /prompts/candidates | POST /prompts/suggest
//   POST /keywords                      (adding a candidate as a keyword)
//
// /prompts/quota is intentionally NOT surfaced (owner request: drop the
// "Quota and remaining budget" section). Backend quota tracking stays,
// since failover decisions depend on it; this file just stops rendering
// it.

import { api } from "../api.js";
import {
  escapeHtml,
  fmtErr,
  notWiredBanner,
  skeletonRows,
  pctOfMax,
  RISK_SECTIONS,
  ensureRiskSections,
  riskSectionsStatus,
} from "../util.js";

// The pipeline, in the order it runs, with each stage explained for the
// person who edits these: a risk analyst, not an engineer. Every "when"
// line answers the question that actually gets asked, which is "if I
// change this, what happens and when".
//
// `editors[].field` matches the backend's own field names
// (app.services.admin.PROMPT_FIELDS), so every editor drives the same
// three generic endpoints and nothing here needs a special case.
const STAGES = [
  {
    n: "1",
    name: "Translate headlines",
    what:
      "Turns Bengali headlines into English. Runs first, on every article whose headline is not already English, so the rest of the pipeline and your review queue read in one language.",
    editors: [
      {
        field: "translation",
        label: "Translation prompt",
        hint:
          "How headlines should be translated. The instruction to answer as a numbered list is added automatically and cannot be edited, because the pipeline reads that list to match each translation back to its article.",
      },
    ],
  },
  {
    n: "2",
    name: "Group duplicates",
    what:
      "Decides which headlines report the same event, so eight outlets carrying one story collapse into one card and you approve it once. Runs after translation, on headlines that already look similar.",
    editors: [
      {
        field: "dedup_verification",
        label: "Duplicate check prompt",
        hint:
          "How strictly two headlines must match to count as the same event. Loosen it and more stories fold together; tighten it and near duplicates stay separate. Nothing is ever deleted by folding, it stays one click away.",
      },
    ],
  },
  {
    n: "3",
    name: "Score relevance",
    what:
      "Gives every article a score from 1 to 10 for how much it matters to Grameenphone risk. The core editorial rules, including the colour bands, are built in and cannot be changed here. These two boxes are added on top of them.",
    editors: [
      {
        field: "guidance",
        label: "Relevance guidance",
        hint: "Added on top of the built-in editorial rules. Applies on the next scoring run.",
        rows: 4,
      },
      {
        field: "examples",
        label: "Scoring examples",
        hint: "Few-shot examples, one per line as <span class=\"mono\">headline =&gt; score</span>.",
        rows: 8,
        mono: true,
      },
    ],
  },
  {
    n: "4",
    name: "Group topics",
    what:
      "Names the theme each scored story belongs to, such as Middle East / oil or Banking / NPLs, then picks one lead story per theme. Runs on articles scoring 5 and above.",
    editors: [
      {
        field: "topic_clustering",
        label: "Topic labelling prompt",
        hint:
          "What counts as a topic and how broad the labels should be. This is the prompt that names the themes. The separate rules for folding stories inside a theme are built in.",
      },
    ],
  },
  {
    n: "5",
    name: "Write brief bullets",
    what:
      "Rewrites each approved story as one bullet for the Daily Risk Insights brief. Runs when you generate the brief, not during a collection run.",
    editors: [
      {
        field: "summarization",
        label: "Bullet writing prompt",
        hint:
          "House style for a bullet: how long, how to open, whether to cite figures. This one is edited end to end, so keep the format instruction if you want the bold hook to keep working.",
      },
    ],
  },
];

const SCORING_FIELDS = ["guidance", "examples"];
const ALL_FIELDS = STAGES.flatMap((st) => st.editors.map((e) => e.field));

export function mountPrompts(root) {
  let alive = true;
  const s = {
    config: null,
    configError: null,
    drafts: {}, // field -> textarea contents
    notes: {}, // field -> { text, ok }
    versions: {}, // field -> [{version, ...}] once loaded; the KEY IS ABSENT until then
    versionsLoading: {}, // field -> bool, in-flight guard for loadVersions
    versionsError: {}, // field -> ApiError, so a failure stops re-firing
    versionsOpen: {}, // field -> bool
    editorOpen: {}, // field -> bool
    candidates: [],
    candidatesError: null,
    suggestBusy: false,
    suggestion: "",
    addingCandidate: null,
    candidateNote: "",
    candidateNoteOk: true,
    forbidden: false,
  };

  render();
  loadConfig();
  loadCandidates();
  // The candidate rows carry a section picker, and its options are the
  // configured sections. This screen is directly routable, so it cannot
  // assume the Dashboard has already been visited: without this it
  // rendered util.js's hardcoded five-name fallback, and adding a
  // candidate keyword filed it under a section that may not exist. A
  // keyword in a non-existent section never matches anything and is
  // invisible until someone wonders why a term stopped working, which
  // is the exact failure the select was introduced to prevent.
  ensureRiskSections().then(() => {
    if (alive) render();
  });

  function noteForbidden(err) {
    if (err && err.kind === "forbidden") s.forbidden = true;
  }

  /** The server's record for one field, whichever shape it arrives in. */
  function stageOf(field) {
    if (!s.config) return null;
    return (s.config.stages || []).find((x) => x.field === field) || null;
  }

  function contentOf(field) {
    if (!s.config) return "";
    if (field === "guidance") return s.config.guidance;
    if (field === "examples") return s.config.examples;
    const st = stageOf(field);
    return st ? st.content : "";
  }

  function versionOf(field) {
    if (!s.config) return null;
    if (field === "guidance") return s.config.guidance_version;
    if (field === "examples") return s.config.examples_version;
    const st = stageOf(field);
    return st ? st.version : null;
  }

  async function loadConfig() {
    try {
      const data = await api.get("/prompts/config");
      if (!alive) return;
      s.config = data;
      // Only refill drafts the user is not currently editing, so a
      // reload triggered by saving one prompt cannot wipe unsaved text
      // sitting in another open editor.
      for (const field of ALL_FIELDS) {
        if (!s.editorOpen[field] || s.drafts[field] === undefined) s.drafts[field] = contentOf(field);
      }
      s.configError = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.configError = err;
      noteForbidden(err);
      render();
    }
  }

  /**
   * Fetch one field's version history.
   *
   * Two guards, both load-bearing, both added after the same panel
   * managed to fail in opposite directions:
   *
   *   `versionsLoading` — renderVersionList() calls this from INSIDE
   *   render(), which is the only way a panel that opens on click can
   *   fetch lazily. Without an in-flight flag, the render that this
   *   function's own `render()` triggers would start a second request
   *   before the first returned.
   *
   *   `versionsError` — this used to swallow the rejection with the
   *   state untouched, so `s.versions[field]` stayed absent, the next
   *   render saw "not loaded yet" and fired again, and that render
   *   failed and fired again: an unbounded stream of failing requests
   *   for as long as the panel stayed open, against an API that allows
   *   120 a minute. Recording the failure ends the loop and gives the
   *   user something to press.
   */
  async function loadVersions(field) {
    if (s.versionsLoading[field]) return;
    s.versionsLoading[field] = true;
    try {
      const data = await api.get(`/prompts/stages/${field}/versions`);
      if (!alive) return;
      s.versions[field] = data || [];
      delete s.versionsError[field];
    } catch (err) {
      if (!alive) return;
      s.versionsError[field] = err;
    } finally {
      s.versionsLoading[field] = false;
    }
    if (alive) render();
  }

  async function loadCandidates() {
    try {
      const data = await api.get("/prompts/candidates");
      if (!alive) return;
      s.candidates = data || [];
      render();
    } catch (err) {
      if (!alive) return;
      s.candidatesError = err;
      render();
    }
  }

  async function savePrompt(field) {
    try {
      await api.put(`/prompts/stages/${field}`, { content: s.drafts[field] });
      // DELETE the key, do not set it to []. renderVersionList() treats
      // "no key" as "not loaded yet" and refetches; `[]` is a loaded,
      // truthy value meaning "this prompt has no history", so assigning
      // it here invalidated nothing and the panel sat on "Loading
      // history..." for the rest of the session, after every single
      // save. The version the user had just created was the one thing
      // they were opening the panel to see.
      delete s.versions[field];
      delete s.versionsError[field];
      // Reload rather than patching the version number locally: the
      // server is the only thing that knows whether what was just saved
      // is actually usable, and an empty or oversized prompt comes back
      // flagged as falling back to the built-in.
      await loadConfig();
      flashNote(field, "Saved", true);
    } catch (err) {
      flashNote(field, "Couldn't save: " + fmtErr(err), false);
    }
  }

  function flashNote(field, text, ok) {
    s.notes[field] = { text, ok };
    render();
    setTimeout(() => {
      if (!alive) return;
      delete s.notes[field];
      render();
    }, 2800);
  }

  async function activateVersion(field, version) {
    try {
      await api.post(`/prompts/stages/${field}/versions/${version}/activate`);
      s.drafts[field] = undefined;
      await loadConfig();
      loadVersions(field);
    } catch (err) {
      flashNote(field, "Couldn't activate: " + fmtErr(err), false);
    }
  }

  function restoreBuiltIn(field) {
    const st = stageOf(field);
    if (!st) return;
    s.drafts[field] = st.default_content;
    flashNote(field, "Built-in prompt loaded. Save it to make it active.", true);
  }

  async function suggest() {
    s.suggestBusy = true;
    render();
    try {
      const res = await api.post("/prompts/suggest");
      s.suggestion = res.suggestion;
    } catch (err) {
      s.suggestion = "";
      // Inline, not modal: this call goes to a live LLM and can take a
      // while, so a rejection arriving as an alert lands long after the
      // click and reads as unrelated.
      flashNote("guidance", "Couldn't get a suggestion: " + fmtErr(err), false);
    }
    s.suggestBusy = false;
    render();
  }

  function applySuggestion() {
    s.drafts.guidance = ((s.drafts.guidance || "") + "\n\n" + s.suggestion).trim();
    s.suggestion = "";
    s.editorOpen.guidance = true;
    savePrompt("guidance");
  }

  function render() {
    root.innerHTML = `
      <div class="page page-narrow" style="max-width:920px;">
        <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0 0 22px;max-width:62ch;">${
          s.config
            ? `Everything that shapes what the AI is asked, stage by stage, learned from <b style="color:var(--tx);">${s.config.approved_count}</b> approved and <b style="color:var(--tx);">${s.config.rejected_count}</b> rejected headlines so far. Which model answers is set on the <b style="color:var(--tx);">AI models</b> page.`
            : "Everything that shapes what the AI is asked, stage by stage."
        }</p>

        ${
          s.forbidden
            ? `<div class="banner banner-error"><span>&#128274;</span><div>This whole console is admin-only. Your account doesn't have the admin role, the server rejected every request here with 403.</div></div>`
            : ""
        }

        ${renderStages()}
        ${renderCandidates()}
      </div>
    `;
    bind();
  }

  // "This screen calls GET /prompts/config, which isn't implemented on
  // the API yet" is a specific claim, and it was being made for EVERY
  // failure: a 500, a dropped connection, a rate-limit. The endpoint is
  // live, so the message was simply false, and because it is phrased as
  // a permanent state it offers nothing to press. The screen stayed
  // stuck until you navigated away. Every other screen already gates
  // that banner on `kind === "not_found"`; these two branches did not.
  function renderStages() {
    if (s.configError) {
      if (s.configError.kind === "forbidden") return "";
      return `<div class="card" style="margin-bottom:16px;">${
        s.configError.kind === "not_found"
          ? notWiredBanner("GET /prompts/config")
          : errorWithRetry(s.configError, "retryConfigBtn")
      }</div>`;
    }
    if (!s.config) return `<div class="card" style="margin-bottom:16px;">${skeletonRows(6, 18)}</div>`;
    return STAGES.map(renderStage).join("");
  }

  function errorWithRetry(err, buttonId) {
    return `<div class="banner banner-error" style="margin:0;flex-direction:column;align-items:stretch;gap:10px;">
      <div class="row" style="gap:8px;"><span>&#10005;</span><div style="flex:1;">${escapeHtml(fmtErr(err))}</div></div>
      <div><button id="${buttonId}" class="btn-sm">Retry</button></div>
    </div>`;
  }

  function renderStage(stage) {
    return `<div class="card" style="margin-bottom:16px;">
      <div class="row" style="gap:12px;align-items:baseline;">
        <span class="mono" style="font-size:11px;color:var(--tx4);">${stage.n}</span>
        <h3 style="margin:0;">${escapeHtml(stage.name)}</h3>
      </div>
      <p class="hint" style="max-width:64ch;">${escapeHtml(stage.what)}</p>
      <div class="stack" style="gap:0;margin-top:14px;">
        ${stage.editors.map(renderEditor).join("")}
      </div>
      ${stage.editors.some((e) => e.field === "guidance") ? renderSuggestBlock() : ""}
    </div>`;
  }

  function renderEditor(editor) {
    const field = editor.field;
    const open = !!s.editorOpen[field];
    const version = versionOf(field);
    const st = stageOf(field);
    const note = s.notes[field];
    const draft = s.drafts[field] !== undefined ? s.drafts[field] : contentOf(field);
    const dirty = draft !== contentOf(field);

    // Two different "not the built-in" signals, and they mean opposite
    // things. `edited` is informational: somebody customised this and it
    // is running. `fallback_reason` is a problem: something was saved,
    // the server refused to send it, and the built-in is running in its
    // place. The second must be visible whether or not the editor is
    // open, because the whole point is that it is easy to miss.
    const edited = st ? !st.is_default : SCORING_FIELDS.includes(field) && !!contentOf(field);
    const broken = st && st.fallback_reason;

    return `<div style="border-top:1px solid var(--line);padding:12px 0 ${open ? "16px" : "12px"};">
      <div class="row-between" style="gap:12px;">
        <div class="row" style="gap:10px;align-items:baseline;flex:1;min-width:0;">
          <span style="font-size:13.5px;font-weight:600;">${escapeHtml(editor.label)}</span>
          ${version === null ? "" : `<span class="mono" style="font-size:10px;color:var(--tx4);">v${version}</span>`}
          ${
            broken
              ? `<span class="pill" style="background:var(--bad-soft,var(--acsoft));color:var(--bad,var(--actx));">using built-in</span>`
              : edited
              ? `<span class="pill" style="background:var(--acsoft);color:var(--actx);">edited</span>`
              : `<span class="pill">built in</span>`
          }
        </div>
        <div class="row" style="gap:9px;">
          ${note ? `<span style="font-size:12.5px;color:${note.ok ? "var(--actx)" : "var(--bad,var(--actx))"};">${escapeHtml(note.text)}</span>` : ""}
          <button class="btn-sm" data-toggle-editor="${escapeHtml(field)}" aria-expanded="${open ? "true" : "false"}">${
      open ? "close" : "edit"
    }</button>
        </div>
      </div>

      ${
        broken
          ? `<div class="banner banner-error" style="margin:10px 0 0;"><span>&#9888;</span><div>The saved version of this prompt is not being used: ${escapeHtml(
              st.fallback_reason
            )}. The built-in prompt is running instead, so this stage still works. Fix the text below and save, or restore the built-in.</div></div>`
          : ""
      }

      ${
        open
          ? `<div style="margin-top:12px;">
              <p class="hint" style="margin-top:0;">${editor.hint}</p>
              <textarea data-draft-field="${escapeHtml(field)}" rows="${editor.rows || 10}" class="textarea-input${
              editor.mono ? " mono" : ""
            }"${
              editor.mono ? ' style="font-size:12px;line-height:1.75;color:var(--tx2);"' : ""
            }>${escapeHtml(draft)}</textarea>
              <div class="row" style="gap:12px;margin-top:12px;flex-wrap:wrap;">
                <button class="btn btn-primary" style="padding:10px 17px;font-size:13px;" data-save-field="${escapeHtml(
                  field
                )}" ${dirty ? "" : "disabled"}>Save</button>
                <button class="btn btn-outline" style="padding:10px 17px;font-size:13px;" data-history-field="${escapeHtml(
                  field
                )}">${s.versionsOpen[field] ? "Hide history" : "Version history"}</button>
                ${
                  st
                    ? `<button class="btn btn-outline" style="padding:10px 17px;font-size:13px;" data-restore-field="${escapeHtml(
                        field
                      )}">Restore built-in</button>`
                    : ""
                }
              </div>
              ${s.versionsOpen[field] ? renderVersionList(field) : ""}
            </div>`
          : ""
      }
    </div>`;
  }

  function renderSuggestBlock() {
    return `<div style="border-top:1px solid var(--line);margin-top:4px;padding-top:14px;">
      <div class="row-between" style="gap:12px;">
        <div style="flex:1;min-width:0;">
          <div style="font-size:13.5px;font-weight:600;">Learn from analyst decisions</div>
          <p class="hint" style="margin:4px 0 0;">Reads approved and rejected headlines and <b style="color:var(--tx);">suggests</b> a refinement. Nothing changes until applied.</p>
        </div>
        <button id="suggestBtn" class="btn btn-soft" style="padding:10px 17px;font-size:13px;white-space:nowrap;" ${
          s.suggestBusy ? "disabled" : ""
        }>${s.suggestBusy ? "Analysing decisions…" : "Suggest a refinement"}</button>
      </div>
      ${
        s.suggestion
          ? `<div style="margin-top:16px;">
              <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);margin-bottom:8px;">Suggested addition</div>
              <div style="background:var(--bg);border:1px solid var(--acline);border-radius:9px;padding:14px 16px;font-size:13.5px;line-height:1.65;color:var(--tx2);">${escapeHtml(
                s.suggestion
              )}</div>
              <div class="row" style="gap:9px;margin-top:12px;">
                <button id="applySuggestionBtn" class="btn btn-primary" style="padding:9px 15px;font-size:12.5px;">Apply to guidance</button>
                <button id="discardSuggestionBtn" class="btn btn-outline" style="padding:9px 15px;font-size:12.5px;">Discard</button>
              </div>
            </div>`
          : ""
      }
    </div>`;
  }

  function renderVersionList(field) {
    const versions = s.versions[field];
    const err = s.versionsError[field];
    if (!versions && !err) loadVersions(field);
    return `<div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px;">
      ${
        err
          ? `<div class="row" style="gap:10px;align-items:center;">
              <span style="flex:1;font-size:12px;color:var(--bad,var(--red));">Couldn't load the history: ${escapeHtml(
                fmtErr(err)
              )}</span>
              <button class="btn-sm" data-retry-versions="${escapeHtml(field)}">Retry</button>
            </div>`
          : versions && versions.length
          ? versions
              .map(
                (v) => `<div class="row-between" style="padding:6px 0;">
          <span class="mono" style="font-size:11px;color:var(--tx3);">v${v.version} · ${escapeHtml(
                  v.created_by
                )} · ${escapeHtml(v.created_at)}${v.note ? " · " + escapeHtml(v.note) : ""}</span>
          ${
            v.active
              ? `<span class="pill" style="background:var(--acsoft);color:var(--actx);">active</span>`
              : `<button class="btn-sm" data-activate-version="${escapeHtml(field)}:${v.version}">activate</button>`
          }
        </div>`
              )
              .join("")
          : versions
          ? // A loaded-but-empty history is a real answer, not a
            // pending one. It renders for a prompt that has never been
            // edited away from the built-in.
            `<div style="font-size:12px;color:var(--tx3);">No earlier versions yet.</div>`
          : `<div style="font-size:12px;color:var(--tx3);">Loading history…</div>`
      }
    </div>`;
  }

  function renderCandidates() {
    return `<div class="card" style="margin-bottom:16px;">
      <h3>Candidate keywords</h3>
      <p class="hint">Words frequent in approved headlines but not keywords yet.</p>
      ${
        s.candidatesError
          ? s.candidatesError.kind === "forbidden"
            ? ""
            : s.candidatesError.kind === "not_found"
            ? notWiredBanner("GET /prompts/candidates")
            : errorWithRetry(s.candidatesError, "retryCandidatesBtn")
          : s.candidates.length
          ? `<div class="stack" style="gap:7px;">${s.candidates
              .map(
                (c) => `<div class="row" style="gap:14px;padding:11px 15px;border:1px solid var(--line);border-radius:10px;background:var(--panel);">
            <span style="flex:0 0 150px;font-size:13.5px;font-weight:600;">${escapeHtml(c.word)}</span>
            <span class="mono" style="flex:0 0 32px;font-size:10.5px;color:var(--tx2);">${escapeHtml(c.lang)}</span>
            <div style="flex:1;display:flex;align-items:center;gap:9px;">
              <div style="flex:1;max-width:180px;height:5px;border-radius:3px;background:var(--gridline);overflow:hidden;"><div style="height:100%;width:${pctOfMax(
                c.approved,
                s.candidates,
                (x) => x.approved
              )};background:var(--ac2);"></div></div>
              <span class="mono" style="font-size:10.5px;color:var(--tx3);">${c.approved} / ${c.rejected}</span>
            </div>
            <select class="text-input" style="padding:5px 8px;font-size:11.5px;width:135px;" aria-label="Section for ${escapeHtml(
              c.word
            )}" data-candidate-section="${escapeHtml(c.word)}" ${RISK_SECTIONS.length ? "" : "disabled"}>
              ${RISK_SECTIONS.map((sec) => `<option value="${escapeHtml(sec)}">${escapeHtml(sec)}</option>`).join("")}
            </select>
            <button class="btn-sm" data-add-candidate="${escapeHtml(c.word)}" data-lang="${escapeHtml(c.lang)}"
                    ${s.addingCandidate === c.word || !RISK_SECTIONS.length ? "disabled" : ""}>${
                  s.addingCandidate === c.word ? "…" : "+ add"
                }</button>
          </div>`
              )
              .join("")}</div>`
          : `<div style="font-size:12px;color:var(--tx3);">No candidates yet.</div>`
      }
      ${
        s.candidates.length && !RISK_SECTIONS.length
          ? `<div style="font-size:12px;color:var(--tx3);margin-top:12px;">${
              riskSectionsStatus.failed
                ? "The section list could not be loaded, so a candidate cannot be filed yet."
                : "No sections are configured yet. Create one on the Keywords screen before adding a candidate."
            }</div>`
          : ""
      }
      ${
        s.candidateNote
          ? `<div class="banner ${s.candidateNoteOk ? "banner-ok" : "banner-error"}" style="margin:14px 0 0;"><span>${
              s.candidateNoteOk ? "&#10003;" : "&#10005;"
            }</span><div style="flex:1;">${escapeHtml(s.candidateNote)}</div></div>`
          : ""
      }
    </div>`;
  }

  /**
   * Add a candidate word as a real keyword.
   *
   * The section used to be collected with window.prompt() showing a
   * comma-joined list, and whatever was typed went straight to the API.
   * A typo produced a keyword filed under a section that does not
   * exist, which then never matches anything and is invisible until
   * someone wonders why a term stopped working. The section is a fixed
   * set, so it is a select.
   */
  async function addCandidate(word, lang) {
    const sel = root.querySelector(`[data-candidate-section="${CSS.escape(word)}"]`);
    const section = (sel && sel.value) || RISK_SECTIONS[0];
    // Belt and braces alongside the disabled button: posting a keyword
    // with no section at all creates a row that matches nothing and is
    // invisible on the Keywords screen's section list.
    if (!section) {
      s.candidateNote = "Pick a section first. None are configured yet.";
      s.candidateNoteOk = false;
      return render();
    }
    s.addingCandidate = word;
    render();
    try {
      await api.post("/keywords", {
        section,
        term_en: lang === "BN" ? "" : word,
        term_bn: lang === "BN" ? word : "",
        enabled: true,
      });
      s.candidateNote = `Added "${word}" to ${section}.`;
      s.candidateNoteOk = true;
      // Drop it from the list: it is no longer a candidate, and leaving
      // it there invites adding the same word twice.
      s.candidates = s.candidates.filter((c) => c.word !== word);
    } catch (err) {
      s.candidateNote = `Couldn't add "${word}": ` + fmtErr(err);
      s.candidateNoteOk = false;
    }
    s.addingCandidate = null;
    render();
    setTimeout(() => {
      if (!alive) return;
      s.candidateNote = "";
      render();
    }, 3200);
  }

  function bind() {
    root.querySelectorAll("[data-draft-field]").forEach((el) => {
      const field = el.getAttribute("data-draft-field");
      el.addEventListener("input", (e) => {
        const wasDirty = s.drafts[field] !== contentOf(field);
        s.drafts[field] = e.target.value;
        // Re-render only when the Save button's enabled state flips,
        // otherwise every keystroke would rebuild the textarea and throw
        // away the caret position.
        if (wasDirty !== (s.drafts[field] !== contentOf(field))) {
          const caret = e.target.selectionStart;
          render();
          const again = root.querySelector(`[data-draft-field="${CSS.escape(field)}"]`);
          if (again) {
            again.focus();
            again.setSelectionRange(caret, caret);
          }
        }
      });
    });

    root.querySelectorAll("[data-toggle-editor]").forEach((b) =>
      b.addEventListener("click", () => {
        const field = b.getAttribute("data-toggle-editor");
        s.editorOpen[field] = !s.editorOpen[field];
        render();
      })
    );
    root.querySelectorAll("[data-save-field]").forEach((b) =>
      b.addEventListener("click", () => savePrompt(b.getAttribute("data-save-field")))
    );
    root.querySelectorAll("[data-history-field]").forEach((b) =>
      b.addEventListener("click", () => {
        const field = b.getAttribute("data-history-field");
        s.versionsOpen[field] = !s.versionsOpen[field];
        render();
      })
    );
    root.querySelectorAll("[data-restore-field]").forEach((b) =>
      b.addEventListener("click", () => restoreBuiltIn(b.getAttribute("data-restore-field")))
    );
    root.querySelectorAll("[data-activate-version]").forEach((b) =>
      b.addEventListener("click", () => {
        const raw = b.getAttribute("data-activate-version");
        const idx = raw.lastIndexOf(":");
        activateVersion(raw.slice(0, idx), Number(raw.slice(idx + 1)));
      })
    );

    root.querySelectorAll("[data-retry-versions]").forEach((b) =>
      b.addEventListener("click", () => {
        const field = b.getAttribute("data-retry-versions");
        delete s.versionsError[field];
        render();
        loadVersions(field);
      })
    );

    const retryConfigBtn = root.querySelector("#retryConfigBtn");
    if (retryConfigBtn)
      retryConfigBtn.addEventListener("click", () => {
        s.configError = null;
        render();
        loadConfig();
      });
    const retryCandidatesBtn = root.querySelector("#retryCandidatesBtn");
    if (retryCandidatesBtn)
      retryCandidatesBtn.addEventListener("click", () => {
        s.candidatesError = null;
        render();
        loadCandidates();
      });

    const suggestBtn = root.querySelector("#suggestBtn");
    if (suggestBtn) suggestBtn.addEventListener("click", suggest);
    const applyBtn = root.querySelector("#applySuggestionBtn");
    if (applyBtn) applyBtn.addEventListener("click", applySuggestion);
    const discardBtn = root.querySelector("#discardSuggestionBtn");
    if (discardBtn)
      discardBtn.addEventListener("click", () => {
        s.suggestion = "";
        render();
      });

    root.querySelectorAll("[data-add-candidate]").forEach((b) =>
      b.addEventListener("click", () => addCandidate(b.getAttribute("data-add-candidate"), b.getAttribute("data-lang")))
    );
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

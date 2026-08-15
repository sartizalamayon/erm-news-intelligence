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
// Real API (live):
//   GET  /prompts/config | PUT /prompts/guidance | PUT /prompts/examples
//   GET  /prompts/{guidance,examples}/versions
//   POST /prompts/{guidance,examples}/versions/{v}/activate
//   GET  /prompts/candidates | POST /prompts/suggest
//   POST /keywords                      (adding a candidate as a keyword)
//
// /prompts/quota is intentionally NOT surfaced (owner request: drop the
// "Quota and remaining budget" section). Backend quota tracking stays,
// since failover decisions depend on it; this file just stops rendering
// it.

import { api } from "../api.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows, pctOfMax, RISK_SECTIONS } from "../util.js";

export function mountPrompts(root) {
  let alive = true;
  const s = {
    config: null,
    configError: null,
    guidanceDraft: "",
    examplesDraft: "",
    savedNote: "",
    versions: { guidance: [], examples: [] },
    versionsOpen: { guidance: false, examples: false },
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

  function noteForbidden(err) {
    if (err && err.kind === "forbidden") s.forbidden = true;
  }

  async function loadConfig() {
    try {
      const data = await api.get("/prompts/config");
      if (!alive) return;
      s.config = data;
      s.guidanceDraft = data.guidance;
      s.examplesDraft = data.examples;
      s.configError = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.configError = err;
      noteForbidden(err);
      render();
    }
  }

  async function loadVersions(kind) {
    try {
      const data = await api.get(`/prompts/${kind}/versions`);
      if (!alive) return;
      s.versions[kind] = data || [];
      render();
    } catch (err) {
      /* history stays empty */
    }
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

  async function saveGuidance() {
    try {
      const v = await api.put("/prompts/guidance", { content: s.guidanceDraft });
      s.config.guidance_version = v.version;
      s.versions.guidance = [];
      flashSaved();
    } catch (err) {
      s.savedNote = "Couldn't save: " + fmtErr(err);
      render();
    }
  }

  async function saveExamples() {
    try {
      const v = await api.put("/prompts/examples", { content: s.examplesDraft });
      s.config.examples_version = v.version;
      s.versions.examples = [];
      flashSaved();
    } catch (err) {
      s.savedNote = "Couldn't save: " + fmtErr(err);
      render();
    }
  }

  function flashSaved() {
    s.savedNote = "✓ Saved";
    render();
    setTimeout(() => {
      s.savedNote = "";
      render();
    }, 2400);
  }

  async function activateVersion(kind, version) {
    try {
      await api.post(`/prompts/${kind}/versions/${version}/activate`);
      loadConfig();
      loadVersions(kind);
    } catch (err) {
      s.savedNote = "Couldn't activate: " + fmtErr(err);
      render();
    }
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
      s.savedNote = "Couldn't get a suggestion: " + fmtErr(err);
    }
    s.suggestBusy = false;
    render();
  }

  function applySuggestion() {
    s.guidanceDraft = (s.guidanceDraft + "\n\n" + s.suggestion).trim();
    s.suggestion = "";
    saveGuidance();
  }

  function render() {
    root.innerHTML = `
      <div class="page page-narrow" style="max-width:920px;">
        <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0 0 22px;max-width:62ch;">${
          s.config
            ? `Everything that shapes what the AI is asked during phase 3, learned from <b style="color:var(--tx);">${s.config.approved_count}</b> approved and <b style="color:var(--tx);">${s.config.rejected_count}</b> rejected headlines so far. Which model answers is set on the <b style="color:var(--tx);">AI models</b> page.`
            : "Everything that shapes what the AI is asked during phase 3."
        }</p>

        ${
          s.forbidden
            ? `<div class="banner banner-error"><span>&#128274;</span><div>This whole console is admin-only. Your account doesn't have the admin role, the server rejected every request here with 403.</div></div>`
            : ""
        }

        ${renderPromptContent()}
        ${renderCandidates()}
      </div>
    `;
    bind();
  }

  function renderPromptContent() {
    if (s.configError) {
      return s.configError.kind === "forbidden" ? "" : `<div class="card" style="margin-bottom:16px;">${notWiredBanner("GET /prompts/config")}</div>`;
    }
    if (!s.config) return `<div class="card" style="margin-bottom:16px;">${skeletonRows(4, 18)}</div>`;
    return `<div class="card" style="margin-bottom:16px;">
        <h3>Relevance guidance <span class="mono" style="font-size:10px;color:var(--tx4);font-weight:400;">v${s.config.guidance_version}</span></h3>
        <p class="hint">Added on top of the built-in editorial rules. Applies on the next scoring run.</p>
        <textarea id="guidanceInput" rows="4" class="textarea-input">${escapeHtml(s.guidanceDraft)}</textarea>
        <div class="row" style="gap:12px;margin-top:12px;">
          <button id="saveGuidanceBtn" class="btn btn-primary" style="padding:10px 17px;font-size:13px;">Save guidance</button>
          <button id="historyGuidanceBtn" class="btn btn-outline" style="padding:10px 17px;font-size:13px;">${s.versionsOpen.guidance ? "Hide history" : "Version history"}</button>
          <span style="font-size:12.5px;color:var(--actx);">${escapeHtml(s.savedNote)}</span>
        </div>
        ${s.versionsOpen.guidance ? renderVersionList("guidance") : ""}
      </div>

      <div class="card" style="margin-bottom:16px;">
        <h3>Scoring examples <span class="mono" style="font-size:10px;color:var(--tx4);font-weight:400;">v${s.config.examples_version}</span></h3>
        <p class="hint">Few-shot examples, one per line as <span class="mono">headline =&gt; score</span>.</p>
        <textarea id="examplesInput" rows="8" class="textarea-input mono" style="font-size:12px;line-height:1.75;color:var(--tx2);">${escapeHtml(s.examplesDraft)}</textarea>
        <div class="row" style="gap:12px;margin-top:12px;">
          <button id="saveExamplesBtn" class="btn btn-primary" style="padding:10px 17px;font-size:13px;">Save examples</button>
          <button id="historyExamplesBtn" class="btn btn-outline" style="padding:10px 17px;font-size:13px;">${s.versionsOpen.examples ? "Hide history" : "Version history"}</button>
        </div>
        ${s.versionsOpen.examples ? renderVersionList("examples") : ""}
      </div>

      <div class="card" style="margin-bottom:16px;">
        <h3>Learn from analyst decisions</h3>
        <p class="hint">Reads approved/rejected headlines and <b style="color:var(--tx);">suggests</b> a refinement. Nothing changes until applied.</p>
        <button id="suggestBtn" class="btn btn-soft" style="padding:10px 17px;font-size:13px;" ${s.suggestBusy ? "disabled" : ""}>${
      s.suggestBusy ? "Analysing decisions…" : "Suggest a refinement"
    }</button>
        ${
          s.suggestion
            ? `<div style="margin-top:16px;">
              <div class="mono" style="font-size:9px;letter-spacing:1.4px;text-transform:uppercase;color:var(--tx3);margin-bottom:8px;">Suggested addition</div>
              <div style="background:var(--bg);border:1px solid var(--acline);border-radius:9px;padding:14px 16px;font-size:13.5px;line-height:1.65;color:var(--tx2);">${escapeHtml(s.suggestion)}</div>
              <div class="row" style="gap:9px;margin-top:12px;">
                <button id="applySuggestionBtn" class="btn btn-primary" style="padding:9px 15px;font-size:12.5px;">Apply to guidance</button>
                <button id="discardSuggestionBtn" class="btn btn-outline" style="padding:9px 15px;font-size:12.5px;">Discard</button>
              </div>
            </div>`
            : ""
        }
      </div>`;
  }

  function renderVersionList(kind) {
    const versions = s.versions[kind];
    if (!versions.length) loadVersions(kind);
    return `<div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px;">
      ${
        versions.length
          ? versions
              .map(
                (v) => `<div class="row-between" style="padding:6px 0;">
          <span class="mono" style="font-size:11px;color:var(--tx3);">v${v.version} · ${escapeHtml(v.created_by)} · ${escapeHtml(v.created_at)}${v.note ? " · " + escapeHtml(v.note) : ""}</span>
          ${v.active ? `<span class="pill" style="background:var(--acsoft);color:var(--actx);">active</span>` : `<button class="btn-sm" data-activate-version="${kind}:${v.version}">activate</button>`}
        </div>`
              )
              .join("")
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
            : notWiredBanner("GET /prompts/candidates")
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
            <select class="text-input" style="padding:5px 8px;font-size:11.5px;width:135px;" data-candidate-section="${escapeHtml(c.word)}">
              ${RISK_SECTIONS.map((sec) => `<option value="${escapeHtml(sec)}">${escapeHtml(sec)}</option>`).join("")}
            </select>
            <button class="btn-sm" data-add-candidate="${escapeHtml(c.word)}" data-lang="${escapeHtml(c.lang)}"
                    ${s.addingCandidate === c.word ? "disabled" : ""}>${s.addingCandidate === c.word ? "…" : "+ add"}</button>
          </div>`
              )
              .join("")}</div>`
          : `<div style="font-size:12px;color:var(--tx3);">No candidates yet.</div>`
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
    const section = sel ? sel.value : RISK_SECTIONS[0];
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
    const guidanceInput = root.querySelector("#guidanceInput");
    if (guidanceInput) guidanceInput.addEventListener("input", (e) => (s.guidanceDraft = e.target.value));
    const examplesInput = root.querySelector("#examplesInput");
    if (examplesInput) examplesInput.addEventListener("input", (e) => (s.examplesDraft = e.target.value));
    const saveGuidanceBtn = root.querySelector("#saveGuidanceBtn");
    if (saveGuidanceBtn) saveGuidanceBtn.addEventListener("click", saveGuidance);
    const saveExamplesBtn = root.querySelector("#saveExamplesBtn");
    if (saveExamplesBtn) saveExamplesBtn.addEventListener("click", saveExamples);
    const historyGuidanceBtn = root.querySelector("#historyGuidanceBtn");
    if (historyGuidanceBtn) historyGuidanceBtn.addEventListener("click", () => { s.versionsOpen.guidance = !s.versionsOpen.guidance; render(); });
    const historyExamplesBtn = root.querySelector("#historyExamplesBtn");
    if (historyExamplesBtn) historyExamplesBtn.addEventListener("click", () => { s.versionsOpen.examples = !s.versionsOpen.examples; render(); });
    root.querySelectorAll("[data-activate-version]").forEach((b) =>
      b.addEventListener("click", () => {
        const [kind, version] = b.getAttribute("data-activate-version").split(":");
        activateVersion(kind, Number(version));
      })
    );

    const suggestBtn = root.querySelector("#suggestBtn");
    if (suggestBtn) suggestBtn.addEventListener("click", suggest);
    const applyBtn = root.querySelector("#applySuggestionBtn");
    if (applyBtn) applyBtn.addEventListener("click", applySuggestion);
    const discardBtn = root.querySelector("#discardSuggestionBtn");
    if (discardBtn) discardBtn.addEventListener("click", () => { s.suggestion = ""; render(); });

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

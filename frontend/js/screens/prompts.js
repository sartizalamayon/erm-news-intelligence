// AI prompts admin (frontend-contract.md §3.9) + provider routing/quota
// (ADR §9.1 DoD #4 — bundled into the same /prompts router by ADMIN-API;
// see app/api/routers/prompts.py's module docstring for why. This is
// real, new-since-the-design-mock surface area: no ERM.dc.html markup
// covers it, so the section below is built fresh rather than recreated
// from the prototype, kept visually consistent with the rest of the app.
//
// Real API — app/api/routers/prompts.py (ADMIN-API, landed):
//   GET  /prompts/config      -> { guidance, examples, guidance_version,
//                                  examples_version, approved_count, rejected_count }
//   PUT  /prompts/guidance    { content, note } -> PromptVersionOut
//   PUT  /prompts/examples    { content, note } -> PromptVersionOut
//   GET  /prompts/guidance/versions | /examples/versions -> PromptVersionOut[]
//   POST /prompts/guidance/versions/{v}/activate | /examples/... -> PromptVersionOut
//   GET  /prompts/candidates  -> [{ word, lang, approved, rejected }]
//   POST /prompts/suggest     -> { suggestion }   (real LLM call — P3-LLM)
//   GET/PATCH /prompts/providers, GET/PUT /prompts/routing, GET/PUT /prompts/quota/{provider}
//
// `candidates[].barWidth` is computed here from the real max `approved`
// count in the returned set (util's pctOfMax) — not the prototype's
// hardcoded `/12` bug (§3.9 / §8 item 4).

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
    candidates: [],
    candidatesError: null,
    suggestBusy: false,
    suggestion: "",
    providers: [],
    providersError: null,
    quota: [],
    editingProvider: null,
  };

  render();
  loadConfig();
  loadCandidates();
  loadProviders();
  loadQuota();

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
      render();
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

  async function loadProviders() {
    try {
      const data = await api.get("/prompts/providers");
      if (!alive) return;
      s.providers = data || [];
      s.providersError = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.providersError = err;
      render();
    }
  }

  async function loadQuota() {
    try {
      const data = await api.get("/prompts/quota");
      if (!alive) return;
      s.quota = data || [];
      render();
    } catch (err) {
      /* quota panel just stays empty */
    }
  }

  async function saveGuidance() {
    try {
      const v = await api.put("/prompts/guidance", { content: s.guidanceDraft });
      s.config.guidance_version = v.version;
      flashSaved();
    } catch (err) {
      s.savedNote = "Couldn't save — " + fmtErr(err);
      render();
    }
  }

  async function saveExamples() {
    try {
      const v = await api.put("/prompts/examples", { content: s.examplesDraft });
      s.config.examples_version = v.version;
      flashSaved();
    } catch (err) {
      s.savedNote = "Couldn't save — " + fmtErr(err);
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

  async function suggest() {
    s.suggestBusy = true;
    render();
    try {
      const res = await api.post("/prompts/suggest");
      s.suggestion = res.suggestion;
    } catch (err) {
      s.suggestion = "";
      window.alert("Couldn't get a suggestion: " + fmtErr(err));
    }
    s.suggestBusy = false;
    render();
  }

  function applySuggestion() {
    s.guidanceDraft = (s.guidanceDraft + "\n\n" + s.suggestion).trim();
    s.suggestion = "";
    saveGuidance();
  }

  async function saveProvider(provider) {
    const model = root.querySelector("#provModel").value.trim();
    const role = root.querySelector("#provRole").value;
    const apiKey = root.querySelector("#provKey").value;
    const body = { model: model || undefined, role: role || undefined };
    if (apiKey) body.api_key = apiKey;
    try {
      await api.patch(`/prompts/providers/${provider}`, body);
      s.editingProvider = null;
      loadProviders();
    } catch (err) {
      window.alert("Couldn't save provider: " + fmtErr(err));
    }
  }

  function render() {
    root.innerHTML = `
      <div class="page page-narrow" style="max-width:880px;">
        <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0 0 22px;max-width:62ch;">${
          s.config
            ? `Everything that shapes phase 3. Based on <b style="color:var(--tx);">${s.config.approved_count}</b> approved and <b style="color:var(--tx);">${s.config.rejected_count}</b> rejected headlines so far.`
            : "Everything that shapes phase 3."
        }</p>

        ${
          s.configError
            ? s.configError.kind === "forbidden"
              ? `<div class="banner banner-error"><span>&#128274;</span><div>AI prompts are admin-only. Your account doesn't have the admin role.</div></div>`
              : notWiredBanner("GET /prompts/config")
            : !s.config
            ? `<div class="card">${skeletonRows(4, 18)}</div>`
            : `<div class="card" style="margin-bottom:16px;">
              <h3>Relevance guidance <span class="mono" style="font-size:10px;color:var(--tx4);font-weight:400;">v${s.config.guidance_version}</span></h3>
              <p class="hint">Added on top of the built-in editorial rules. Write additions or overrides in your own words — applies on the next scoring run.</p>
              <textarea id="guidanceInput" rows="4" class="textarea-input">${escapeHtml(s.guidanceDraft)}</textarea>
              <div class="row" style="gap:12px;margin-top:12px;">
                <button id="saveGuidanceBtn" class="btn btn-primary" style="padding:10px 17px;font-size:13px;">Save guidance</button>
                <span style="font-size:12.5px;color:var(--actx);">${escapeHtml(s.savedNote)}</span>
              </div>
            </div>

            <div class="card" style="margin-bottom:16px;">
              <h3>Scoring examples <span class="mono" style="font-size:10px;color:var(--tx4);font-weight:400;">v${s.config.examples_version}</span></h3>
              <p class="hint">Few-shot examples that teach your standard, one per line as <span class="mono">headline =&gt; score</span>.</p>
              <textarea id="examplesInput" rows="8" class="textarea-input mono" style="font-size:12px;line-height:1.75;color:var(--tx2);">${escapeHtml(s.examplesDraft)}</textarea>
              <div style="margin-top:12px;">
                <button id="saveExamplesBtn" class="btn btn-primary" style="padding:10px 17px;font-size:13px;">Save examples</button>
              </div>
            </div>

            <div class="card" style="margin-bottom:16px;">
              <h3>Learn from analyst decisions</h3>
              <p class="hint">Reads the approved and rejected headlines and <b style="color:var(--tx);">suggests</b> a refinement. Nothing changes until you review it and apply.</p>
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
            </div>`
        }

        <div class="card" style="margin-bottom:16px;">
          <h3>Candidate keywords</h3>
          <p class="hint">Words frequent in approved headlines but not keywords yet. High approved, low rejected is the strong signal.</p>
          ${
            s.candidatesError
              ? s.candidatesError.kind === "not_found"
                ? notWiredBanner("GET /prompts/candidates")
                : `<div class="banner banner-error"><span>&#10005;</span><div>${escapeHtml(fmtErr(s.candidatesError))}</div></div>`
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
                <button class="btn-sm" data-add-candidate="${escapeHtml(c.word)}" data-lang="${escapeHtml(c.lang)}">+ add</button>
              </div>`
                  )
                  .join("")}</div>`
              : `<div style="font-size:12px;color:var(--tx3);">No candidates yet.</div>`
          }
        </div>

        ${renderProviders()}
      </div>
    `;
    bind();
  }

  function renderProviders() {
    if (s.providersError && s.providersError.kind === "not_found") {
      return `<div class="card">${notWiredBanner("GET /prompts/providers")}</div>`;
    }
    if (s.providersError && s.providersError.kind === "forbidden") return "";
    return `<div class="card">
      <h3>Model provider routing & quota</h3>
      <p class="hint">Admin-configurable per ADR §9.1 — API keys are write-only (never echoed back) and encrypted at rest.</p>
      <div class="stack" style="gap:9px;">
        ${s.providers
          .map((p) => {
            const q = s.quota.find((x) => x.provider === p.provider);
            const editing = s.editingProvider === p.provider;
            return `<div style="border:1px solid var(--line);border-radius:10px;background:var(--panel2);padding:13px 15px;">
            <div class="row-between wrap">
              <div>
                <span style="font-size:13px;font-weight:600;">${escapeHtml(p.provider)}</span>
                <span class="mono" style="font-size:11px;color:var(--tx3);margin-left:8px;">${escapeHtml(p.model)} · ${escapeHtml(p.role)}</span>
              </div>
              <div class="row" style="gap:8px;">
                <span class="mono" style="font-size:10.5px;color:${p.has_key ? "var(--green)" : "var(--tx4)"};">${p.has_key ? "key set (…" + escapeHtml(p.api_key_last4) + ")" : "no key"}</span>
                <button class="btn-sm" data-edit-provider="${p.provider}">${editing ? "close" : "edit"}</button>
              </div>
            </div>
            ${
              q
                ? `<div class="mono" style="font-size:10.5px;color:var(--tx3);margin-top:8px;">tokens ${q.tokens_used.toLocaleString()}${
                    q.tpd ? " / " + q.tpd.toLocaleString() + " (" + (q.tokens_used_pct || 0).toFixed(0) + "%)" : ""
                  } · requests ${q.requests_used}${q.rpd ? " / " + q.rpd : ""}${q.exhausted ? " · EXHAUSTED" : ""}</div>`
                : ""
            }
            ${
              editing
                ? `<div class="stack" style="gap:8px;margin-top:12px;max-width:420px;">
                <input id="provModel" class="text-input" style="padding:8px 11px;font-size:12.5px;" placeholder="model" value="${escapeHtml(p.model)}" />
                <select id="provRole" class="text-input" style="padding:8px 11px;font-size:12.5px;">
                  ${["primary", "secondary", "off"].map((r) => `<option value="${r}" ${p.role === r ? "selected" : ""}>${r}</option>`).join("")}
                </select>
                <input id="provKey" type="password" class="text-input" style="padding:8px 11px;font-size:12.5px;" placeholder="New API key (leave blank to keep current)" autocomplete="off" />
                <button class="btn btn-primary" data-save-provider="${p.provider}" style="padding:8px 14px;font-size:12px;align-self:flex-start;">Save</button>
              </div>`
                : ""
            }
          </div>`;
          })
          .join("") || `<div style="font-size:12px;color:var(--tx3);">No providers configured.</div>`}
      </div>
    </div>`;
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
    const suggestBtn = root.querySelector("#suggestBtn");
    if (suggestBtn) suggestBtn.addEventListener("click", suggest);
    const applyBtn = root.querySelector("#applySuggestionBtn");
    if (applyBtn) applyBtn.addEventListener("click", applySuggestion);
    const discardBtn = root.querySelector("#discardSuggestionBtn");
    if (discardBtn) discardBtn.addEventListener("click", () => { s.suggestion = ""; render(); });

    root.querySelectorAll("[data-add-candidate]").forEach((b) =>
      b.addEventListener("click", async () => {
        const word = b.getAttribute("data-add-candidate");
        const lang = b.getAttribute("data-lang");
        const section = window.prompt("Which section? (" + RISK_SECTIONS.join(", ") + ")", RISK_SECTIONS[0]);
        if (!section) return;
        try {
          await api.post("/keywords", {
            section,
            term_en: lang === "BN" ? "" : word,
            term_bn: lang === "BN" ? word : "",
            enabled: true,
          });
          window.alert("Added “" + word + "” to " + section + ".");
        } catch (err) {
          window.alert("Couldn't add: " + fmtErr(err));
        }
      })
    );

    root.querySelectorAll("[data-edit-provider]").forEach((b) =>
      b.addEventListener("click", () => {
        const p = b.getAttribute("data-edit-provider");
        s.editingProvider = s.editingProvider === p ? null : p;
        render();
      })
    );
    root.querySelectorAll("[data-save-provider]").forEach((b) => b.addEventListener("click", () => saveProvider(b.getAttribute("data-save-provider"))));
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

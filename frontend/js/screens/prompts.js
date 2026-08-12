// AI prompts + provider/routing/credential admin console
// (frontend-contract.md §3.9 + ADR §9.1 DoD #4, owner requirement: all
// provider/AI config must be configurable from the website). Bundled
// under /prompts by ADMIN-API — see app/api/routers/prompts.py.
//
// Real API (live):
//   GET  /prompts/config | PUT guidance | PUT examples
//   GET  /prompts/{guidance,examples}/versions
//   POST /prompts/{guidance,examples}/versions/{v}/activate
//   GET  /prompts/candidates | POST /prompts/suggest
//   GET/PUT /prompts/routing            { routes: { <feature>: { primary:{provider,model}, secondary:{provider,model}|null } } }
//     features: scoring, translation, dedup_verification, topic_clustering, summarization
//   GET  /prompts/provider-definitions  -> [{provider,adapter,base_url,default_model,enabled,note}]
//   PUT  /prompts/provider-definitions/{provider}  (upsert — add or edit; provider name is free text)
//   GET  /prompts/provider-definitions/{provider}/credentials -> [{provider,key_id,api_key_last4}]
//   POST   same path { key_id, api_key } -> masked out, never echoes plaintext
//   DELETE .../credentials/{key_id}
//
// /prompts/quota is intentionally NOT surfaced on this screen (owner
// request: drop the "Quota & remaining budget" section). Backend quota
// tracking (app.services.llm.quota) stays — other things depend on it
// for failover decisions — this file just stops rendering it.
//
// Provider name / model / base_url are free text everywhere on purpose
// (provider-limits-VERIFIED.md: model IDs go dead without notice — must
// be runtime config, never a hardcoded dropdown). Only `adapter` is a
// fixed choice (openai_compatible | gemini) — that's a wire-protocol
// shape, not a vendor list. Keys are write-only: never rendered back.

import { api } from "../api.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows, pctOfMax, RISK_SECTIONS } from "../util.js";

const FEATURES = ["scoring", "translation", "dedup_verification", "topic_clustering", "summarization"];
const ADAPTERS = ["openai_compatible", "gemini"];

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
    routing: null,
    routingError: null,
    routingDraft: null,
    routingSaving: false,
    routingNote: "",
    providers: [],
    providersError: null,
    addingProvider: false,
    expandedProvider: null,
    credentials: {},
    forbidden: false,
  };

  render();
  loadConfig();
  loadCandidates();
  loadRouting();
  loadProviders();

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

  async function loadRouting() {
    try {
      const data = await api.get("/prompts/routing");
      if (!alive) return;
      s.routing = data.routes || {};
      s.routingDraft = JSON.parse(JSON.stringify(s.routing));
      s.routingError = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.routingError = err;
      noteForbidden(err);
      render();
    }
  }

  async function loadProviders() {
    try {
      const data = await api.get("/prompts/provider-definitions");
      if (!alive) return;
      s.providers = data || [];
      s.providersError = null;
      render();
    } catch (err) {
      if (!alive) return;
      s.providersError = err;
      noteForbidden(err);
      render();
    }
  }

  async function loadCredentials(provider) {
    try {
      const data = await api.get(`/prompts/provider-definitions/${encodeURIComponent(provider)}/credentials`);
      if (!alive) return;
      s.credentials[provider] = data || [];
      render();
    } catch (err) {
      s.credentials[provider] = [];
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
      s.savedNote = "Couldn't save — " + fmtErr(err);
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

  async function activateVersion(kind, version) {
    try {
      await api.post(`/prompts/${kind}/versions/${version}/activate`);
      loadConfig();
      loadVersions(kind);
    } catch (err) {
      window.alert("Couldn't activate: " + fmtErr(err));
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

  async function saveRouting() {
    s.routingSaving = true;
    render();
    try {
      const body = { routes: s.routingDraft };
      const data = await api.put("/prompts/routing", body);
      s.routing = data.routes;
      s.routingDraft = JSON.parse(JSON.stringify(s.routing));
      s.routingNote = "✓ Routing saved";
    } catch (err) {
      s.routingNote = "Couldn't save — " + fmtErr(err);
    }
    s.routingSaving = false;
    render();
    setTimeout(() => {
      s.routingNote = "";
      render();
    }, 2400);
  }

  function providerNames() {
    return s.providers.map((p) => p.provider);
  }

  async function saveProviderDef(existing) {
    const nameInput = root.querySelector("#pdName");
    const provider = existing ? existing.provider : nameInput.value.trim();
    if (!provider) return window.alert("Provider name is required.");
    const adapter = root.querySelector("#pdAdapter").value;
    const base_url = root.querySelector("#pdBaseUrl").value.trim() || null;
    const default_model = root.querySelector("#pdModel").value.trim();
    const enabled = root.querySelector("#pdEnabled").checked;
    const note = root.querySelector("#pdNote").value.trim();
    if (!default_model) return window.alert("Default model is required.");
    try {
      await api.put(`/prompts/provider-definitions/${encodeURIComponent(provider)}`, { adapter, base_url, default_model, enabled, note });
      s.addingProvider = false;
      s.expandedProvider = null;
      loadProviders();
    } catch (err) {
      window.alert("Couldn't save provider: " + fmtErr(err));
    }
  }

  async function toggleProviderEnabled(p) {
    try {
      await api.put(`/prompts/provider-definitions/${encodeURIComponent(p.provider)}`, {
        adapter: p.adapter,
        base_url: p.base_url,
        default_model: p.default_model,
        enabled: !p.enabled,
        note: p.note,
      });
      loadProviders();
    } catch (err) {
      window.alert("Couldn't update: " + fmtErr(err));
    }
  }

  async function addCredential(provider) {
    const keyId = root.querySelector("#credKeyId").value.trim();
    const apiKey = root.querySelector("#credApiKey").value;
    if (!keyId || !apiKey) return window.alert("Key label and API key are both required.");
    try {
      await api.post(`/prompts/provider-definitions/${encodeURIComponent(provider)}/credentials`, { key_id: keyId, api_key: apiKey });
      root.querySelector("#credKeyId").value = "";
      root.querySelector("#credApiKey").value = "";
      loadCredentials(provider);
    } catch (err) {
      window.alert("Couldn't add key: " + fmtErr(err));
    }
  }

  async function deleteCredential(provider, keyId) {
    if (!window.confirm(`Delete key "${keyId}" for ${provider}?`)) return;
    try {
      await api.delete(`/prompts/provider-definitions/${encodeURIComponent(provider)}/credentials/${encodeURIComponent(keyId)}`);
      loadCredentials(provider);
    } catch (err) {
      window.alert("Couldn't delete: " + fmtErr(err));
    }
  }

  function render() {
    root.innerHTML = `
      <div class="page page-narrow" style="max-width:920px;">
        <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0 0 22px;max-width:62ch;">${
          s.config
            ? `Everything that shapes phase 3. Based on <b style="color:var(--tx);">${s.config.approved_count}</b> approved and <b style="color:var(--tx);">${s.config.rejected_count}</b> rejected headlines so far.`
            : "Everything that shapes phase 3, plus provider/model routing and credentials."
        }</p>

        ${
          s.forbidden
            ? `<div class="banner banner-error"><span>&#128274;</span><div>This whole console is admin-only. Your account doesn't have the admin role — the server rejected every request here with 403.</div></div>`
            : ""
        }

        ${renderPromptContent()}
        ${renderCandidates()}
        ${renderRouting()}
        ${renderProviders()}
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
            <button class="btn-sm" data-add-candidate="${escapeHtml(c.word)}" data-lang="${escapeHtml(c.lang)}">+ add</button>
          </div>`
              )
              .join("")}</div>`
          : `<div style="font-size:12px;color:var(--tx3);">No candidates yet.</div>`
      }
    </div>`;
  }

  function providerOptions(selected) {
    const names = providerNames();
    if (selected && !names.includes(selected)) names.push(selected);
    return names.map((p) => `<option value="${escapeHtml(p)}" ${p === selected ? "selected" : ""}>${escapeHtml(p)}</option>`).join("");
  }

  function renderRouting() {
    if (s.routingError && s.routingError.kind === "forbidden") return "";
    if (s.routingError) return `<div class="card" style="margin-bottom:16px;">${notWiredBanner("GET /prompts/routing")}</div>`;
    if (!s.routingDraft) return `<div class="card" style="margin-bottom:16px;"><h3>Model routing</h3>${skeletonRows(5, 20)}</div>`;
    return `<div class="card" style="margin-bottom:16px;">
      <h3>Model routing per feature</h3>
      <p class="hint">Which provider (and optionally which model) handles each pipeline feature — independently, not as a failover chain.</p>
      <div class="stack" style="gap:10px;">
        ${FEATURES.map((f) => {
          const route = s.routingDraft[f] || { primary: { provider: "", model: "" }, secondary: null };
          const hasSecondary = !!route.secondary;
          return `<div style="border:1px solid var(--line);border-radius:10px;background:var(--panel2);padding:13px 15px;">
          <div style="font-size:13px;font-weight:600;margin-bottom:10px;">${escapeHtml(f.replace(/_/g, " "))}</div>
          <div class="row wrap" style="gap:20px;">
            <div>
              <div class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx3);margin-bottom:6px;">Primary</div>
              <div class="row" style="gap:6px;">
                <select class="text-input" style="padding:7px 9px;font-size:12.5px;width:150px;" data-route-primary-provider="${f}">${providerOptions(route.primary?.provider)}</select>
                <input class="text-input" style="padding:7px 9px;font-size:12.5px;width:150px;" placeholder="model override" value="${escapeHtml(route.primary?.model || "")}" data-route-primary-model="${f}" />
              </div>
            </div>
            <div>
              <div class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx3);margin-bottom:6px;">Secondary <label style="font-weight:400;text-transform:none;letter-spacing:0;"><input type="checkbox" data-route-secondary-toggle="${f}" ${hasSecondary ? "checked" : ""} style="margin-left:6px;vertical-align:middle;" /></label></div>
              ${
                hasSecondary
                  ? `<div class="row" style="gap:6px;">
                <select class="text-input" style="padding:7px 9px;font-size:12.5px;width:150px;" data-route-secondary-provider="${f}">${providerOptions(route.secondary?.provider)}</select>
                <input class="text-input" style="padding:7px 9px;font-size:12.5px;width:150px;" placeholder="model override" value="${escapeHtml(route.secondary?.model || "")}" data-route-secondary-model="${f}" />
              </div>`
                  : `<div style="font-size:11.5px;color:var(--tx4);padding-top:8px;">none</div>`
              }
            </div>
          </div>
        </div>`;
        }).join("")}
      </div>
      <div class="row" style="gap:12px;margin-top:14px;">
        <button id="saveRoutingBtn" class="btn btn-primary" style="padding:10px 17px;font-size:13px;" ${s.routingSaving ? "disabled" : ""}>${s.routingSaving ? "Saving…" : "Save routing"}</button>
        <span style="font-size:12.5px;color:var(--actx);">${escapeHtml(s.routingNote)}</span>
      </div>
    </div>`;
  }

  function renderProviders() {
    if (s.providersError && s.providersError.kind === "forbidden") return "";
    if (s.providersError) return `<div class="card" style="margin-bottom:16px;">${notWiredBanner("GET /prompts/provider-definitions")}</div>`;
    return `<div class="card" style="margin-bottom:16px;">
      <div class="row-between" style="margin-bottom:4px;">
        <h3 style="margin:0;">Providers</h3>
        <button id="addProviderBtn" class="btn btn-outline" style="padding:8px 14px;font-size:12.5px;">+ Add provider</button>
      </div>
      <p class="hint">Add any provider by name — Mistral, NVIDIA NIM, Groq, Gemini, or one that doesn't exist yet. Model IDs are free text.</p>
      ${s.addingProvider ? providerForm(null) : ""}
      <div class="stack" style="gap:9px;">
        ${
          s.providers.length
            ? s.providers.map((p) => providerRow(p)).join("")
            : `<div style="font-size:12px;color:var(--tx3);">No providers defined yet.</div>`
        }
      </div>
    </div>`;
  }

  function providerRow(p) {
    const expanded = s.expandedProvider === p.provider;
    if (expanded && !(p.provider in s.credentials)) loadCredentials(p.provider);
    return `<div style="border:1px solid var(--line);border-radius:10px;background:var(--panel2);padding:13px 15px;">
      <div class="row-between wrap">
        <div>
          <span style="font-size:13px;font-weight:600;">${escapeHtml(p.provider)}</span>
          <span class="mono" style="font-size:11px;color:var(--tx3);margin-left:8px;">${escapeHtml(p.adapter)} · ${escapeHtml(p.default_model)}${p.base_url ? " · " + escapeHtml(p.base_url) : ""}</span>
        </div>
        <div class="row" style="gap:8px;">
          <span class="pill" style="background:${p.enabled ? "var(--acsoft)" : "var(--gridline)"};color:${p.enabled ? "var(--actx)" : "var(--tx3)"};"><span class="pill-dot" style="background:${p.enabled ? "var(--ac2)" : "var(--tx4)"}"></span>${p.enabled ? "enabled" : "disabled"}</span>
          <button class="btn-sm" data-toggle-provider="${escapeHtml(p.provider)}">${p.enabled ? "disable" : "enable"}</button>
          <button class="btn-sm" data-credentials-provider="${escapeHtml(p.provider)}">${expanded ? "close" : "keys"}</button>
        </div>
      </div>
      ${p.note ? `<div style="font-size:11.5px;color:var(--tx3);margin-top:6px;">${escapeHtml(p.note)}</div>` : ""}
      ${expanded ? credentialsPanel(p.provider) : ""}
    </div>`;
  }

  function credentialsPanel(provider) {
    const keys = s.credentials[provider] || [];
    return `<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line);">
      <div class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx3);margin-bottom:8px;">Pooled API keys</div>
      ${
        keys.length
          ? `<div class="stack" style="gap:6px;margin-bottom:10px;">${keys
              .map(
                (k) => `<div class="row-between" style="font-size:12px;">
            <span class="mono" style="color:var(--tx2);">${escapeHtml(k.key_id)} — …${escapeHtml(k.api_key_last4)}</span>
            <button class="btn-sm btn-sm-danger" data-delete-cred="${escapeHtml(provider)}:${escapeHtml(k.key_id)}">delete</button>
          </div>`
              )
              .join("")}</div>`
          : `<div style="font-size:12px;color:var(--tx3);margin-bottom:10px;">No keys stored.</div>`
      }
      <div class="row" style="gap:6px;">
        <input id="credKeyId" class="text-input" style="padding:7px 9px;font-size:12px;width:140px;" placeholder="label (e.g. key-1)" />
        <input id="credApiKey" type="password" class="text-input" style="padding:7px 9px;font-size:12px;flex:1;" placeholder="API key" autocomplete="off" />
        <button class="btn btn-primary" data-add-cred="${escapeHtml(provider)}" style="padding:7px 14px;font-size:12px;">Add key</button>
      </div>
    </div>`;
  }

  function providerForm(existing) {
    return `<div style="border:1px solid var(--acline);border-radius:10px;background:var(--acsoft);padding:14px 15px;margin-bottom:12px;">
      <div class="stack" style="gap:8px;max-width:480px;">
        <input id="pdName" class="text-input" style="padding:8px 11px;font-size:12.5px;" placeholder="Provider name (e.g. mistral)" ${existing ? `value="${escapeHtml(existing.provider)}" disabled` : ""} />
        <select id="pdAdapter" class="text-input" style="padding:8px 11px;font-size:12.5px;">${ADAPTERS.map((a) => `<option value="${a}" ${existing && existing.adapter === a ? "selected" : ""}>${a}</option>`).join("")}</select>
        <input id="pdBaseUrl" class="text-input" style="padding:8px 11px;font-size:12.5px;" placeholder="Base URL (optional)" value="${existing ? escapeHtml(existing.base_url || "") : ""}" />
        <input id="pdModel" class="text-input" style="padding:8px 11px;font-size:12.5px;" placeholder="Default model (free text)" value="${existing ? escapeHtml(existing.default_model) : ""}" />
        <input id="pdNote" class="text-input" style="padding:8px 11px;font-size:12.5px;" placeholder="Note (optional)" value="${existing ? escapeHtml(existing.note || "") : ""}" />
        <label style="font-size:12.5px;display:flex;align-items:center;gap:8px;"><input type="checkbox" id="pdEnabled" ${!existing || existing.enabled ? "checked" : ""} /> Enabled</label>
        <div class="row" style="gap:8px;">
          <button id="pdSave" class="btn btn-primary" style="padding:8px 14px;font-size:12.5px;">Save</button>
          <button id="pdCancel" class="btn btn-outline" style="padding:8px 14px;font-size:12.5px;">Cancel</button>
        </div>
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
      b.addEventListener("click", async () => {
        const word = b.getAttribute("data-add-candidate");
        const lang = b.getAttribute("data-lang");
        const section = window.prompt("Which section? (" + RISK_SECTIONS.join(", ") + ")", RISK_SECTIONS[0]);
        if (!section) return;
        try {
          await api.post("/keywords", { section, term_en: lang === "BN" ? "" : word, term_bn: lang === "BN" ? word : "", enabled: true });
          window.alert("Added “" + word + "” to " + section + ".");
        } catch (err) {
          window.alert("Couldn't add: " + fmtErr(err));
        }
      })
    );

    // Routing
    root.querySelectorAll("[data-route-primary-provider]").forEach((el) =>
      el.addEventListener("change", (e) => {
        const f = el.getAttribute("data-route-primary-provider");
        ensureRoute(f).primary.provider = e.target.value;
      })
    );
    root.querySelectorAll("[data-route-primary-model]").forEach((el) =>
      el.addEventListener("input", (e) => {
        const f = el.getAttribute("data-route-primary-model");
        ensureRoute(f).primary.model = e.target.value || null;
      })
    );
    root.querySelectorAll("[data-route-secondary-provider]").forEach((el) =>
      el.addEventListener("change", (e) => {
        const f = el.getAttribute("data-route-secondary-provider");
        ensureRoute(f).secondary.provider = e.target.value;
      })
    );
    root.querySelectorAll("[data-route-secondary-model]").forEach((el) =>
      el.addEventListener("input", (e) => {
        const f = el.getAttribute("data-route-secondary-model");
        ensureRoute(f).secondary.model = e.target.value || null;
      })
    );
    root.querySelectorAll("[data-route-secondary-toggle]").forEach((el) =>
      el.addEventListener("change", (e) => {
        const f = el.getAttribute("data-route-secondary-toggle");
        const route = ensureRoute(f);
        route.secondary = e.target.checked ? { provider: providerNames()[0] || "", model: "" } : null;
        render();
      })
    );
    const saveRoutingBtn = root.querySelector("#saveRoutingBtn");
    if (saveRoutingBtn) saveRoutingBtn.addEventListener("click", saveRouting);

    // Providers
    const addProviderBtn = root.querySelector("#addProviderBtn");
    if (addProviderBtn) addProviderBtn.addEventListener("click", () => { s.addingProvider = !s.addingProvider; render(); });
    const pdSave = root.querySelector("#pdSave");
    if (pdSave) pdSave.addEventListener("click", () => saveProviderDef(null));
    const pdCancel = root.querySelector("#pdCancel");
    if (pdCancel) pdCancel.addEventListener("click", () => { s.addingProvider = false; render(); });
    root.querySelectorAll("[data-toggle-provider]").forEach((b) =>
      b.addEventListener("click", () => toggleProviderEnabled(s.providers.find((p) => p.provider === b.getAttribute("data-toggle-provider"))))
    );
    root.querySelectorAll("[data-credentials-provider]").forEach((b) =>
      b.addEventListener("click", () => {
        const p = b.getAttribute("data-credentials-provider");
        s.expandedProvider = s.expandedProvider === p ? null : p;
        render();
      })
    );
    const addCredBtn = root.querySelector("[data-add-cred]");
    if (addCredBtn) addCredBtn.addEventListener("click", () => addCredential(addCredBtn.getAttribute("data-add-cred")));
    root.querySelectorAll("[data-delete-cred]").forEach((b) =>
      b.addEventListener("click", () => {
        const [provider, keyId] = b.getAttribute("data-delete-cred").split(":");
        deleteCredential(provider, keyId);
      })
    );
  }

  function ensureRoute(feature) {
    if (!s.routingDraft[feature]) s.routingDraft[feature] = { primary: { provider: providerNames()[0] || "", model: "" }, secondary: null };
    if (!s.routingDraft[feature].primary) s.routingDraft[feature].primary = { provider: "", model: "" };
    return s.routingDraft[feature];
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

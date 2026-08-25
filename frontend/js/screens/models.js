// AI models: providers, their API keys, and which model handles what.
//
// This was the bottom half of the AI prompts screen. The two halves
// answer completely different questions and are edited by different
// people at different times: prompts are editorial ("what counts as
// relevant"), this is infrastructure ("which vendor, which key, which
// model"). Stacking them meant scrolling past four editorial cards to
// change a model id, and the routing controls sat below a candidate
// keyword list they have nothing to do with.
//
// Endpoints (app/api/routers/prompts.py, unchanged by the split):
//   GET/PUT /prompts/routing
//     { routes: { <feature>: { primary:{provider,model}, secondary:{provider,model}|null } } }
//   GET  /prompts/provider-definitions
//   PUT  /prompts/provider-definitions/{provider}     upsert
//   DELETE   /prompts/provider-definitions/{provider}     removes it + its keys
//   GET/POST /prompts/provider-definitions/{provider}/credentials
//   DELETE   /prompts/provider-definitions/{provider}/credentials/{key_id}
//
// Provider name, model and base_url are free text on purpose
// (provider-limits-VERIFIED.md: model ids go dead without notice, so
// they must be runtime config and never a hardcoded dropdown). Only
// `adapter` is a fixed choice, because that is a wire-protocol shape
// rather than a vendor list. Keys are write-only and never rendered
// back: the API returns only the last four characters.
//
// WHAT THIS SCREEN ADDS over the section it replaces:
//
//   1. Broken routes are named. A route can point at a provider that is
//      DISABLED, or at one with NO API KEYS stored. Both fail at run
//      time, in a background thread, as a stage error. Nothing on the
//      old screen hinted at either, even though both are visible from
//      data the page already loads. This is not hypothetical: the
//      `cerebras` provider is disabled precisely because it has no
//      free tier, and routing something to it is one dropdown away.
//
//   2. Providers can be edited. The old form was add-only: `pdSave`
//      always called saveProviderDef(null), so the `existing` branch
//      was unreachable. Changing a default model meant re-adding the
//      provider under the same name and relying on PUT being an upsert,
//      which nothing on the screen said.
//
//   3. Failures are inline. Every error path here used window.alert,
//      which is modal, unstyled, and suppressed outright in some
//      embeds, so a rejected save could look like a dead button.

import { api } from "../api.js";
import { escapeHtml, fmtErr, notWiredBanner, skeletonRows } from "../util.js";

const FEATURES = ["scoring", "translation", "dedup_verification", "topic_clustering", "summarization"];
const ADAPTERS = ["openai_compatible", "gemini"];

/** What each routed feature actually does, so the label is not the only clue. */
const FEATURE_DESC = {
  scoring: "Rates each matched story 1 to 10 for relevance.",
  translation: "Turns Bengali headlines into English.",
  dedup_verification: "Confirms two stories really are the same event.",
  topic_clustering: "Groups scored articles into broad topics.",
  summarization: "Writes the short summary shown on a story.",
};

export function mountModels(root) {
  let alive = true;
  const s = {
    routing: null,
    routingDraft: null,
    routingError: null,
    routingSaving: false,
    note: "",
    noteOk: true,
    providers: [],
    providersError: null,
    credentials: {}, // provider -> [{key_id, api_key_last4}]
    addingProvider: false,
    probing: null, // provider name currently being probed
    probeResult: null, // { provider, ok, detail }
    editingProvider: null, // provider name being edited
    expandedProvider: null,
    forbidden: false,
    loading: true,
  };

  render();
  loadAll();

  async function loadAll() {
    s.loading = true;
    render();
    const [rt, pv] = await Promise.allSettled([api.get("/prompts/routing"), api.get("/prompts/provider-definitions")]);
    if (!alive) return;

    if (rt.status === "fulfilled") {
      s.routing = rt.value.routes || {};
      s.routingDraft = structuredClone(s.routing);
      s.routingError = null;
    } else {
      s.routingError = rt.reason;
      noteForbidden(rt.reason);
    }
    if (pv.status === "fulfilled") {
      s.providers = pv.value || [];
      s.providersError = null;
    } else {
      s.providersError = pv.reason;
      noteForbidden(pv.reason);
    }
    s.loading = false;
    render();

    // Key counts are what make the "routed to a provider with no keys"
    // check possible, so they are loaded for EVERY provider rather than
    // only the expanded one. Five small requests, once per visit.
    await Promise.all(s.providers.map((p) => loadCredentials(p.provider)));
  }

  function noteForbidden(err) {
    if (err && err.kind === "forbidden") s.forbidden = true;
  }

  async function loadCredentials(provider) {
    try {
      const data = await api.get(`/prompts/provider-definitions/${encodeURIComponent(provider)}/credentials`);
      if (!alive) return;
      s.credentials[provider] = data || [];
    } catch (err) {
      // A credentials read that fails is not the same as "no keys": say
      // so rather than reporting a false zero, which would make the
      // warning below accuse a healthy provider.
      s.credentials[provider] = null;
    }
    render();
  }

  function flash(message, ok = true) {
    s.note = message;
    s.noteOk = ok;
    render();
    setTimeout(() => {
      if (!alive) return;
      s.note = "";
      render();
    }, 3200);
  }

  // ── derived ──────────────────────────────────────────────────────────

  function providerNames() {
    return s.providers.map((p) => p.provider);
  }

  function providerByName(name) {
    return s.providers.find((p) => p.provider === name) || null;
  }

  function keyCount(name) {
    const keys = s.credentials[name];
    return Array.isArray(keys) ? keys.length : null; // null: not known yet
  }

  /**
   * Why a single routing target cannot work, or "" if it can.
   *
   * Both failures below surface at run time as a stage error in a
   * background thread, which is the worst place to learn about a
   * dropdown you set last week.
   */
  function targetProblem(target) {
    if (!target || !target.provider) return "no provider selected";
    const p = providerByName(target.provider);
    if (!p) return `provider "${target.provider}" is not defined`;
    if (!p.enabled) return `provider "${target.provider}" is disabled`;
    if (p.config_problem) return `provider "${target.provider}" is misconfigured`;
    if (keyCount(target.provider) === 0) return `provider "${target.provider}" has no API keys stored`;
    return "";
  }

  /** Every broken route, as readable sentences. */
  function routingProblems() {
    if (!s.routingDraft) return [];
    const out = [];
    for (const f of FEATURES) {
      const route = s.routingDraft[f];
      if (!route) {
        out.push(`${label(f)} has no route configured.`);
        continue;
      }
      const primary = targetProblem(route.primary);
      if (primary) out.push(`${label(f)} primary: ${primary}.`);
      // A broken SECONDARY is a warning, not a failure: the primary
      // still works and the fallback simply never fires. Said plainly
      // so it is not mistaken for an outage.
      if (route.secondary) {
        const secondary = targetProblem(route.secondary);
        if (secondary) out.push(`${label(f)} fallback: ${secondary}. The primary still works.`);
      }
    }
    return out;
  }

  function label(feature) {
    return feature.replace(/_/g, " ");
  }

  // ── actions ──────────────────────────────────────────────────────────

  async function saveRouting() {
    s.routingSaving = true;
    render();
    try {
      const data = await api.put("/prompts/routing", { routes: s.routingDraft });
      s.routing = data.routes;
      s.routingDraft = structuredClone(s.routing);
      flash("Routing saved.", true);
    } catch (err) {
      flash("Couldn't save routing: " + fmtErr(err), false);
    }
    s.routingSaving = false;
    render();
  }

  async function saveProviderDef() {
    const editing = s.editingProvider ? providerByName(s.editingProvider) : null;
    const provider = editing ? editing.provider : root.querySelector("#pdName").value.trim();
    const adapter = root.querySelector("#pdAdapter").value;
    const base_url = root.querySelector("#pdBaseUrl").value.trim() || null;
    const default_model = root.querySelector("#pdModel").value.trim();
    const enabled = root.querySelector("#pdEnabled").checked;
    const note = root.querySelector("#pdNote").value.trim();

    const errEl = root.querySelector("#pdError");
    const showError = (msg) => {
      if (!errEl) return;
      errEl.textContent = msg;
      errEl.style.display = "";
    };
    if (errEl) errEl.style.display = "none";
    if (!provider) return showError("Provider name is required.");
    if (!default_model) return showError("Default model is required.");
    // Adding a name that already exists silently overwrote that
    // provider, because PUT is an upsert. Nothing said so.
    if (!editing && providerByName(provider)) {
      return showError(`A provider called "${provider}" already exists. Use its edit button instead.`);
    }

    try {
      await api.put(`/prompts/provider-definitions/${encodeURIComponent(provider)}`, {
        adapter,
        base_url,
        default_model,
        enabled,
        note,
      });
      s.addingProvider = false;
      s.editingProvider = null;
      await loadAll();
      flash(`Saved ${provider}.`, true);
    } catch (err) {
      showError("Couldn't save: " + fmtErr(err));
    }
  }

  /**
   * Which routed features point at this provider, if any.
   *
   * The server refuses the delete in that case, but saying so BEFORE
   * the click is what stops the operator from having to guess which of
   * five features to re-point. Same data the routing warnings use.
   */
  function featuresRoutedTo(name) {
    if (!s.routingDraft) return [];
    return FEATURES.filter((f) => {
      const route = s.routingDraft[f];
      if (!route) return false;
      return [route.primary, route.secondary].some((t) => t && t.provider === name);
    }).map(label);
  }

  /**
   * Make one real call through this provider and say what happened.
   *
   * Until this existed, the only way to discover a broken provider was
   * to run the pipeline and read the articles. A base_url that 404'd
   * therefore went unnoticed across a whole day's translation, because
   * every stage substitutes a neutral default on failure.
   */
  async function probeProvider(p) {
    s.probing = p.provider;
    s.probeResult = null;
    render();
    try {
      const res = await api.post(`/prompts/provider-definitions/${encodeURIComponent(p.provider)}/probe`);
      s.probeResult = { provider: p.provider, ok: !!res.ok, detail: res.detail || "" };
    } catch (err) {
      s.probeResult = { provider: p.provider, ok: false, detail: fmtErr(err) };
    }
    s.probing = null;
    render();
  }

  async function deleteProvider(p) {
    const routed = featuresRoutedTo(p.provider);
    if (routed.length) {
      return flash(
        `${p.provider} still handles ${routed.join(", ")}. Point those at another provider first.`,
        false
      );
    }
    const keys = keyCount(p.provider);
    const warning =
      `Delete ${p.provider}?` +
      (keys ? `\n\nIts ${keys} stored API key${keys === 1 ? "" : "s"} will be deleted too.` : "") +
      "\n\nThis cannot be undone.";
    if (!window.confirm(warning)) return;
    try {
      await api.delete(`/prompts/provider-definitions/${encodeURIComponent(p.provider)}`);
      await loadAll();
      flash(`Deleted ${p.provider}.`, true);
    } catch (err) {
      flash(`Couldn't delete ${p.provider}: ` + fmtErr(err), false);
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
      await loadAll();
    } catch (err) {
      flash(`Couldn't update ${p.provider}: ` + fmtErr(err), false);
    }
  }

  async function addCredential(provider) {
    const keyIdEl = root.querySelector("#credKeyId");
    const apiKeyEl = root.querySelector("#credApiKey");
    const errEl = root.querySelector("#credError");
    const showError = (msg) => {
      if (!errEl) return;
      errEl.textContent = msg;
      errEl.style.display = "";
    };
    if (errEl) errEl.style.display = "none";
    if (!keyIdEl.value.trim() || !apiKeyEl.value) {
      return showError("A label and an API key are both required.");
    }
    try {
      await api.post(`/prompts/provider-definitions/${encodeURIComponent(provider)}/credentials`, {
        key_id: keyIdEl.value.trim(),
        api_key: apiKeyEl.value,
      });
      keyIdEl.value = "";
      apiKeyEl.value = "";
      await loadCredentials(provider);
      flash(`Key added to ${provider}.`, true);
    } catch (err) {
      showError("Couldn't add key: " + fmtErr(err));
    }
  }

  async function deleteCredential(provider, keyId) {
    if (!window.confirm(`Delete key "${keyId}" for ${provider}?`)) return;
    try {
      await api.delete(
        `/prompts/provider-definitions/${encodeURIComponent(provider)}/credentials/${encodeURIComponent(keyId)}`
      );
      await loadCredentials(provider);
    } catch (err) {
      flash("Couldn't delete key: " + fmtErr(err), false);
    }
  }

  // ── fragments ────────────────────────────────────────────────────────

  function problemsBanner() {
    const problems = routingProblems();
    if (!problems.length) return "";
    return `<div class="banner banner-warn" style="align-items:flex-start;">
      <span>&#9888;</span>
      <div style="flex:1;">
        <div style="font-weight:600;margin-bottom:6px;">${problems.length} routing problem${problems.length === 1 ? "" : "s"}</div>
        <ul style="margin:0;padding-left:18px;font-size:12.5px;line-height:1.7;">
          ${problems.map((p) => `<li>${escapeHtml(p)}</li>`).join("")}
        </ul>
        <div style="font-size:11.5px;margin-top:8px;opacity:.85;">These fail during a run, inside a background stage. Fix them here rather than reading it off a stage error later.</div>
      </div>
    </div>`;
  }

  function providerOptions(selected) {
    const names = providerNames();
    // A route can name a provider that has since been deleted. Keeping
    // it in the list is what lets the warning above say so instead of
    // the select silently snapping to an unrelated vendor.
    if (selected && !names.includes(selected)) names.push(selected);
    return names
      .map((name) => {
        const p = providerByName(name);
        const suffix = !p ? " (not defined)" : !p.enabled ? " (disabled)" : "";
        return `<option value="${escapeHtml(name)}" ${name === selected ? "selected" : ""}>${escapeHtml(name + suffix)}</option>`;
      })
      .join("");
  }

  function targetControls(feature, slot, target) {
    const problem = targetProblem(target);
    return `<div class="row" style="gap:6px;">
        <select class="text-input" style="padding:7px 9px;font-size:12.5px;width:160px;${problem ? "border-color:var(--amber);" : ""}"
                data-route-provider="${feature}" data-slot="${slot}">${providerOptions(target?.provider)}</select>
        <input class="text-input" style="padding:7px 9px;font-size:12.5px;width:170px;" placeholder="model override"
               aria-label="Model override for the ${slot} provider on ${label(feature)}"
               value="${escapeHtml(target?.model || "")}" data-route-model="${feature}" data-slot="${slot}" />
      </div>
      ${problem ? `<div style="font-size:11px;color:var(--amber);margin-top:5px;">${escapeHtml(problem)}</div>` : ""}`;
  }

  /**
   * A red banner with a Retry for anything that is not a 404.
   *
   * Both cards on this screen used to branch two ways only: `forbidden`
   * (handled by the lock banner at the top, so render nothing here) or
   * else `notWiredBanner(...)`. That second banner tells the reader the
   * route "isn't implemented on the API yet" and that the screen will
   * populate itself once it ships. For a 500, a 429 or a dropped
   * connection every word of that is false, and it is a particularly
   * expensive falsehood on THIS screen: an admin who opens AI models
   * during an incident, sees "not implemented yet" where the provider
   * list should be, and concludes the routing console was never
   * finished, has been told the opposite of what is happening. The
   * banner also carries no control, so the screen stays empty until
   * they navigate away and come back.
   *
   * `review.js` and `sources.js` set the pattern: gate that banner on
   * `err.kind === "not_found"`, give everything else the error text and
   * a Retry. `loadAll()` refetches both halves, which is the right
   * granularity here because the two settle together.
   */
  function loadFailureCard(err, endpointLabel, style = "") {
    if (err.kind === "not_found") return `<div class="card"${style}>${notWiredBanner(endpointLabel)}</div>`;
    return `<div class="card"${style}>
      <div class="banner banner-error" style="margin-bottom:0;"><span>&#10005;</span><div style="flex:1;">${escapeHtml(
        fmtErr(err)
      )}</div><button type="button" class="btn-ghost js-models-retry" style="color:var(--red);text-decoration:underline;">Retry</button></div>
    </div>`;
  }
  // A CLASS, not an id: routing and providers are two independent
  // requests that fail together far more often than separately (both
  // are admin routes on the same API), so this markup can legitimately
  // appear twice on one page. Two nodes sharing an id would leave the
  // second Retry bound to nothing, which is exactly the sort of dead
  // control this fix exists to remove.

  function routingCard() {
    if (s.routingError) {
      if (s.routingError.kind === "forbidden") return "";
      return loadFailureCard(s.routingError, "GET /prompts/routing", ` style="margin-bottom:16px;"`);
    }
    if (!s.routingDraft) return `<div class="card" style="margin-bottom:16px;"><h3>Model routing</h3>${skeletonRows(5, 20)}</div>`;

    const dirty = JSON.stringify(s.routing) !== JSON.stringify(s.routingDraft);
    return `<div class="card" style="margin-bottom:16px;">
      <h3 style="margin:0 0 4px;">Model routing</h3>
      <p class="hint" style="margin:0 0 14px;">
        Which provider handles each pipeline feature. Each feature is routed independently, not as
        one failover chain, so a slow vendor on one stage does not drag the others.
        Leave the model box empty to use that provider's default.
      </p>
      <div class="stack" style="gap:10px;">
        ${FEATURES.map((f) => {
          const route = s.routingDraft[f] || { primary: { provider: "", model: "" }, secondary: null };
          const hasSecondary = !!route.secondary;
          return `<div style="border:1px solid var(--line);border-radius:10px;background:var(--panel2);padding:13px 15px;">
            <div style="font-size:13px;font-weight:600;text-transform:capitalize;">${escapeHtml(label(f))}</div>
            <div style="font-size:11.5px;color:var(--tx3);margin:2px 0 10px;">${escapeHtml(FEATURE_DESC[f] || "")}</div>
            <div class="row wrap" style="gap:22px;align-items:flex-start;">
              <div>
                <div class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx3);margin-bottom:6px;">Primary</div>
                ${targetControls(f, "primary", route.primary)}
              </div>
              <div>
                <label class="mono row" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx3);margin-bottom:6px;gap:6px;align-items:center;cursor:pointer;">
                  <input type="checkbox" data-route-secondary-toggle="${f}" ${hasSecondary ? "checked" : ""} style="accent-color:var(--ac2);" />
                  Fallback
                </label>
                ${
                  hasSecondary
                    ? targetControls(f, "secondary", route.secondary)
                    : `<div style="font-size:11.5px;color:var(--tx4);padding-top:8px;">Not set. If the primary fails, the stage fails.</div>`
                }
              </div>
            </div>
          </div>`;
        }).join("")}
      </div>
      <div class="row" style="gap:12px;margin-top:14px;align-items:center;">
        <button id="saveRoutingBtn" class="btn btn-primary" style="padding:10px 17px;font-size:13px;" ${s.routingSaving ? "disabled" : ""}>${
          s.routingSaving ? "Saving…" : "Save routing"
        }</button>
        ${dirty ? `<span style="font-size:12px;color:var(--amber);">Unsaved changes</span>` : ""}
      </div>
    </div>`;
  }

  function keyBadge(name) {
    const n = keyCount(name);
    if (n === null) return `<span class="mono" style="font-size:10.5px;color:var(--tx4);">keys unknown</span>`;
    if (n === 0) return `<span class="mono" style="font-size:10.5px;color:var(--amber);">no keys</span>`;
    return `<span class="mono" style="font-size:10.5px;color:var(--tx3);">${n} key${n === 1 ? "" : "s"}</span>`;
  }

  function providerRow(p) {
    if (s.editingProvider === p.provider) return providerForm(p);
    const expanded = s.expandedProvider === p.provider;
    // Disabled with a reason on hover, rather than enabled and then
    // rejected by the server: the operator needs to know WHICH features
    // to re-point, and a 422 after the click does not say.
    const routedHere = featuresRoutedTo(p.provider);
    return `<div style="border:1px solid var(--line);border-radius:10px;background:var(--panel2);padding:13px 15px;">
      <div class="row-between wrap" style="gap:10px;">
        <div style="min-width:0;">
          <span style="font-size:13px;font-weight:600;">${escapeHtml(p.provider)}</span>
          <span class="mono" style="font-size:11px;color:var(--tx3);margin-left:8px;">${escapeHtml(p.adapter)} · ${escapeHtml(p.default_model)}</span>
        </div>
        <div class="row" style="gap:8px;align-items:center;">
          ${keyBadge(p.provider)}
          <button class="btn-sm" data-probe-provider="${escapeHtml(p.provider)}" ${s.probing ? "disabled" : ""}>${
            s.probing === p.provider ? "testing…" : "test"
          }</button>
          <span class="pill" style="background:${p.enabled ? "var(--acsoft)" : "var(--gridline)"};color:${p.enabled ? "var(--actx)" : "var(--tx3)"};"><span class="pill-dot" style="background:${p.enabled ? "var(--ac2)" : "var(--tx4)"}"></span>${p.enabled ? "enabled" : "disabled"}</span>
          <button class="btn-sm" data-edit-provider="${escapeHtml(p.provider)}">edit</button>
          <button class="btn-sm" data-toggle-provider="${escapeHtml(p.provider)}">${p.enabled ? "disable" : "enable"}</button>
          <button class="btn-sm" data-credentials-provider="${escapeHtml(p.provider)}">${expanded ? "close" : "keys"}</button>
          <button class="btn-sm btn-sm-danger" data-delete-provider="${escapeHtml(p.provider)}"
                  ${routedHere.length ? `disabled title="Still handles ${escapeHtml(routedHere.join(", "))}"` : ""}>delete</button>
        </div>
      </div>
      ${p.base_url ? `<div class="mono" style="font-size:10.5px;color:var(--tx4);margin-top:5px;">${escapeHtml(p.base_url)}</div>` : ""}
      ${
        p.config_problem
          ? `<div class="banner banner-error" style="margin:8px 0 0;"><span>&#10005;</span><div style="flex:1;font-size:12px;">${escapeHtml(p.config_problem)}</div></div>`
          : ""
      }
      ${p.note ? `<div style="font-size:11.5px;color:var(--tx3);margin-top:6px;line-height:1.5;">${escapeHtml(p.note)}</div>` : ""}
      ${
        s.probeResult && s.probeResult.provider === p.provider
          ? `<div class="banner ${s.probeResult.ok ? "banner-ok" : "banner-error"}" style="margin:10px 0 0;">
              <span>${s.probeResult.ok ? "&#10003;" : "&#10005;"}</span>
              <div style="flex:1;font-size:12px;word-break:break-word;">${escapeHtml(s.probeResult.detail)}</div>
            </div>`
          : ""
      }
      ${expanded ? credentialsPanel(p.provider) : ""}
    </div>`;
  }

  /**
   * The pooled-key panel for one provider.
   *
   * THE TWO ADD-KEY BOXES NOW HAVE REAL LABELS. They had none at all,
   * only placeholders, and a placeholder is not a label: it is gone the
   * moment there is a character in the box, and it is not reliably
   * announced. On the password field that matters more than usual,
   * because the value is masked, so once the placeholder disappears
   * there is nothing on screen or in the accessibility tree saying
   * which of the two boxes holds the secret and which holds its name.
   * `tests/frontend/a11y.test.mjs` now fails on an input with no
   * accessible name, so this cannot quietly come back.
   *
   * The placeholder on the key field changed from "API key" to "paste
   * the key" so it says something the label does not, rather than
   * repeating it.
   *
   * The ids stay unique on the page because only one provider is
   * expanded at a time (`s.expandedProvider`).
   */
  function credentialsPanel(provider) {
    const keys = s.credentials[provider];
    return `<div style="margin-top:12px;padding-top:12px;border-top:1px solid var(--line);">
      <div class="mono" style="font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--tx3);margin-bottom:8px;">Pooled API keys</div>
      <p class="hint" style="margin:0 0 10px;">
        Several keys for one provider are used in rotation, which raises the effective rate limit.
        Keys are write-only: once saved, only the last four characters are ever shown again.
      </p>
      ${
        keys === null
          ? `<div style="font-size:12px;color:var(--amber);margin-bottom:10px;">Couldn't read the stored keys.</div>`
          : keys.length
          ? `<div class="stack" style="gap:6px;margin-bottom:10px;">${keys
              .map(
                (k) => `<div class="row-between" style="font-size:12px;">
            <span class="mono" style="color:var(--tx2);">${escapeHtml(k.key_id)}: …${escapeHtml(k.api_key_last4)}</span>
            <button class="btn-sm btn-sm-danger" data-delete-cred="${escapeHtml(provider)}" data-key-id="${escapeHtml(k.key_id)}">delete</button>
          </div>`
              )
              .join("")}</div>`
          : `<div style="font-size:12px;color:var(--amber);margin-bottom:10px;">No keys stored. Anything routed to ${escapeHtml(provider)} will fail.</div>`
      }
      <div id="credError" role="alert" aria-live="assertive"
           style="display:none;font-size:12px;color:var(--red);background:color-mix(in oklab, var(--red) 12%, transparent);border:1px solid var(--red);border-radius:6px;padding:7px 10px;margin-bottom:8px;"></div>
      <div class="row wrap" style="gap:6px;align-items:flex-end;">
        <div style="width:150px;">
          <label class="field-label" for="credKeyId">Key label</label>
          <input id="credKeyId" class="text-input" style="padding:7px 9px;font-size:12px;width:100%;" placeholder="label, e.g. key-1" />
        </div>
        <div style="flex:1;min-width:180px;">
          <label class="field-label" for="credApiKey">API key</label>
          <input id="credApiKey" type="password" class="text-input" style="padding:7px 9px;font-size:12px;width:100%;" placeholder="paste the key" autocomplete="off" />
        </div>
        <button class="btn btn-primary" data-add-cred="${escapeHtml(provider)}" style="padding:7px 14px;font-size:12px;">Add key</button>
      </div>
    </div>`;
  }

  function providerForm(existing) {
    return `<div style="border:1px solid var(--acline);border-radius:10px;background:var(--acsoft);padding:14px 15px;margin-bottom:12px;">
      <div class="mono" style="font-size:9px;letter-spacing:1.2px;text-transform:uppercase;color:var(--tx3);margin-bottom:10px;">${
        existing ? "Edit " + escapeHtml(existing.provider) : "New provider"
      }</div>
      <div class="stack" style="gap:8px;max-width:480px;">
        <div>
          <label class="field-label" for="pdName">Name</label>
          <input id="pdName" class="text-input" style="padding:8px 11px;font-size:12.5px;" placeholder="e.g. mistral"
                 value="${existing ? escapeHtml(existing.provider) : ""}" ${existing ? "disabled" : ""} />
        </div>
        <div>
          <label class="field-label" for="pdAdapter">Adapter, the wire protocol this vendor speaks</label>
          <select id="pdAdapter" class="text-input" style="padding:8px 11px;font-size:12.5px;">${ADAPTERS.map(
            (a) => `<option value="${a}" ${existing && existing.adapter === a ? "selected" : ""}>${a}</option>`
          ).join("")}</select>
        </div>
        <div>
          <label class="field-label" for="pdBaseUrl">Base URL, the API root</label>
          <input id="pdBaseUrl" class="text-input" style="padding:8px 11px;font-size:12.5px;"
                 placeholder="https://api.example.com/v1"
                 value="${existing ? escapeHtml(existing.base_url || "") : ""}" />
          <div style="font-size:11px;color:var(--tx4);margin-top:4px;line-height:1.45;">
            The root only. <span class="mono">/chat/completions</span> is added for you, so pasting a full endpoint from a vendor's docs will 404.
          </div>
        </div>
        <div>
          <label class="field-label" for="pdModel">Default model, free text</label>
          <input id="pdModel" class="text-input" style="padding:8px 11px;font-size:12.5px;" value="${existing ? escapeHtml(existing.default_model) : ""}" />
        </div>
        <div>
          <label class="field-label" for="pdNote">Note, optional</label>
          <input id="pdNote" class="text-input" style="padding:8px 11px;font-size:12.5px;" value="${existing ? escapeHtml(existing.note || "") : ""}" />
        </div>
        <label style="font-size:12.5px;display:flex;align-items:center;gap:8px;cursor:pointer;">
          <input type="checkbox" id="pdEnabled" style="accent-color:var(--ac2);" ${!existing || existing.enabled ? "checked" : ""} /> Enabled
        </label>
        <div id="pdError" role="alert" aria-live="assertive"
             style="display:none;font-size:12px;color:var(--red);background:color-mix(in oklab, var(--red) 12%, transparent);border:1px solid var(--red);border-radius:6px;padding:7px 10px;"></div>
        <div class="row" style="gap:8px;">
          <button id="pdSave" class="btn btn-primary" style="padding:8px 14px;font-size:12.5px;">Save</button>
          <button id="pdCancel" class="btn btn-outline" style="padding:8px 14px;font-size:12.5px;">Cancel</button>
        </div>
      </div>
    </div>`;
  }

  function providersCard() {
    if (s.providersError) {
      if (s.providersError.kind === "forbidden") return "";
      return loadFailureCard(s.providersError, "GET /prompts/provider-definitions");
    }
    return `<div class="card">
      <div class="row-between" style="margin-bottom:4px;">
        <h3 style="margin:0;">Providers</h3>
        <button id="addProviderBtn" class="btn btn-outline" style="padding:8px 14px;font-size:12.5px;">+ Add provider</button>
      </div>
      <p class="hint" style="margin:0 0 14px;">
        Any vendor that speaks one of the two adapters below, including one that does not exist yet.
        Model ids are free text because vendors retire them without notice.
      </p>
      ${s.addingProvider ? providerForm(null) : ""}
      <div class="stack" style="gap:9px;">
        ${
          s.loading
            ? skeletonRows(4, 44)
            : s.providers.length
            ? s.providers.map(providerRow).join("")
            : `<div style="font-size:12px;color:var(--tx3);">No providers defined yet.</div>`
        }
      </div>
    </div>`;
  }

  // ── render ───────────────────────────────────────────────────────────

  function render() {
    root.innerHTML = `<div class="page page-narrow" style="max-width:920px;">
      <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0 0 18px;max-width:62ch;">
        Which AI vendor runs which part of the pipeline, and the keys they use.
        Editorial guidance lives on the AI prompts page; nothing here changes what the models are asked,
        only who answers.
      </p>

      ${
        s.forbidden
          ? `<div class="banner banner-error"><span>&#128274;</span><div>This console is admin only. The server rejected every request here with 403.</div></div>`
          : ""
      }

      ${problemsBanner()}

      ${
        s.note
          ? `<div class="banner ${s.noteOk ? "banner-ok" : "banner-error"}"><span>${s.noteOk ? "&#10003;" : "&#10005;"}</span><div style="flex:1;">${escapeHtml(s.note)}</div></div>`
          : ""
      }

      ${routingCard()}
      ${providersCard()}
    </div>`;
    bind();
  }

  // ── binding ──────────────────────────────────────────────────────────

  function ensureRoute(feature) {
    if (!s.routingDraft[feature]) {
      s.routingDraft[feature] = { primary: { provider: providerNames()[0] || "", model: null }, secondary: null };
    }
    if (!s.routingDraft[feature].primary) s.routingDraft[feature].primary = { provider: "", model: null };
    return s.routingDraft[feature];
  }

  function bind() {
    root.querySelectorAll(".js-models-retry").forEach((b) => b.addEventListener("click", () => loadAll()));

    // Routing. Provider changes re-render so the warning next to the
    // select updates immediately; model text does NOT re-render, because
    // rebuilding the DOM on every keystroke is what tore the focused
    // input out from under the caret on the review queue.
    root.querySelectorAll("[data-route-provider]").forEach((el) =>
      el.addEventListener("change", (e) => {
        const f = el.getAttribute("data-route-provider");
        const slot = el.getAttribute("data-slot");
        const route = ensureRoute(f);
        if (slot === "secondary" && !route.secondary) route.secondary = { provider: "", model: null };
        route[slot].provider = e.target.value;
        render();
      })
    );
    root.querySelectorAll("[data-route-model]").forEach((el) =>
      el.addEventListener("input", (e) => {
        const f = el.getAttribute("data-route-model");
        const slot = el.getAttribute("data-slot");
        const route = ensureRoute(f);
        if (slot === "secondary" && !route.secondary) route.secondary = { provider: "", model: null };
        route[slot].model = e.target.value.trim() || null;
      })
    );
    root.querySelectorAll("[data-route-secondary-toggle]").forEach((el) =>
      el.addEventListener("change", (e) => {
        const route = ensureRoute(el.getAttribute("data-route-secondary-toggle"));
        route.secondary = e.target.checked ? { provider: providerNames()[0] || "", model: null } : null;
        render();
      })
    );
    const saveRoutingBtn = root.querySelector("#saveRoutingBtn");
    if (saveRoutingBtn) saveRoutingBtn.addEventListener("click", saveRouting);

    // Providers
    const addProviderBtn = root.querySelector("#addProviderBtn");
    if (addProviderBtn)
      addProviderBtn.addEventListener("click", () => {
        s.addingProvider = !s.addingProvider;
        s.editingProvider = null;
        render();
      });
    const pdSave = root.querySelector("#pdSave");
    if (pdSave) pdSave.addEventListener("click", saveProviderDef);
    const pdCancel = root.querySelector("#pdCancel");
    if (pdCancel)
      pdCancel.addEventListener("click", () => {
        s.addingProvider = false;
        s.editingProvider = null;
        render();
      });

    root.querySelectorAll("[data-edit-provider]").forEach((b) =>
      b.addEventListener("click", () => {
        s.editingProvider = b.getAttribute("data-edit-provider");
        s.addingProvider = false;
        s.expandedProvider = null;
        render();
      })
    );
    root.querySelectorAll("[data-toggle-provider]").forEach((b) =>
      b.addEventListener("click", () => toggleProviderEnabled(providerByName(b.getAttribute("data-toggle-provider"))))
    );
    root.querySelectorAll("[data-probe-provider]").forEach((b) =>
      b.addEventListener("click", () => probeProvider(providerByName(b.getAttribute("data-probe-provider"))))
    );
    root.querySelectorAll("[data-delete-provider]").forEach((b) =>
      b.addEventListener("click", () => deleteProvider(providerByName(b.getAttribute("data-delete-provider"))))
    );
    root.querySelectorAll("[data-credentials-provider]").forEach((b) =>
      b.addEventListener("click", () => {
        const p = b.getAttribute("data-credentials-provider");
        s.expandedProvider = s.expandedProvider === p ? null : p;
        s.editingProvider = null;
        render();
      })
    );

    const addCredBtn = root.querySelector("[data-add-cred]");
    if (addCredBtn) addCredBtn.addEventListener("click", () => addCredential(addCredBtn.getAttribute("data-add-cred")));
    root.querySelectorAll("[data-delete-cred]").forEach((b) =>
      b.addEventListener("click", () =>
        // Split on a delimiter, as the old code did, breaks the moment a
        // key label contains that delimiter. Two attributes cannot.
        deleteCredential(b.getAttribute("data-delete-cred"), b.getAttribute("data-key-id"))
      )
    );
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

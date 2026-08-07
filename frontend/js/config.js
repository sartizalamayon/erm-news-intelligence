// API base URL — configuration, not hardcoded (mission DoD #8).
//
// Resolution order:
//   1. `?api=https://...` query param (persisted to localStorage, then
//      the param is stripped from the visible URL).
//   2. `localStorage['erm_api_base']`, if previously set.
//   3. Same-origin heuristic: localhost/127.0.0.1/file:// -> local dev
//      API; anything else -> DEFAULT_PROD_API below.
//
// This means: `python -m http.server` locally against a local
// `uvicorn app.main:app --reload` works with zero configuration, and a
// Render Static Site deploy works by editing exactly one constant
// (DEFAULT_PROD_API) below once the Render web service URL is known —
// no build step, no env-substitution tooling required.

const STORAGE_KEY = "erm_api_base";
const DEFAULT_DEV_API = "http://localhost:8000";

// EDIT ME at deploy time: the Render web service's public URL (ADR §1 —
// API host is a Render free web service, distinct origin from the
// Render Static Site hosting this frontend).
const DEFAULT_PROD_API = "https://erm-backend.onrender.com";

function resolveApiBase() {
  try {
    const params = new URLSearchParams(window.location.search);
    if (params.has("api")) {
      const val = params.get("api").replace(/\/+$/, "");
      if (val) {
        localStorage.setItem(STORAGE_KEY, val);
        params.delete("api");
        const rest = params.toString();
        const newUrl = window.location.pathname + (rest ? "?" + rest : "") + window.location.hash;
        window.history.replaceState(null, "", newUrl);
      }
    }
  } catch (err) {
    // URLSearchParams / history can throw in exotic embeds — never let
    // config resolution itself break the app.
    console.warn("erm: could not read ?api= override", err);
  }

  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) return stored;
  } catch (err) {
    // localStorage disabled (private browsing / embedded webview) — fall
    // through to the heuristic default.
  }

  const host = window.location.hostname;
  const isLocal = host === "localhost" || host === "127.0.0.1" || window.location.protocol === "file:";
  return isLocal ? DEFAULT_DEV_API : DEFAULT_PROD_API;
}

export const API_BASE = resolveApiBase();

export function setApiBase(url) {
  try {
    localStorage.setItem(STORAGE_KEY, url.replace(/\/+$/, ""));
  } catch (err) {
    /* ignore */
  }
  window.location.reload();
}

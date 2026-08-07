// Thin fetch wrapper shared by every screen. Handles cookies
// (SameSite=None; Secure in production per ADR §1 — API and static
// site are different Render origins), JSON in/out, and classifies
// errors so screens can render loading/empty/error states distinctly
// (frontend-contract.md §4) instead of every failure looking the same.

import { API_BASE } from "./config.js";

export class ApiError extends Error {
  constructor(message, { status = 0, kind = "error", body = null } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.kind = kind; // "network" | "not_found" | "forbidden" | "unauthorized" | "rate_limited" | "server" | "validation" | "error"
    this.body = body;
  }
}

let onUnauthorized = null;
export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn;
}

function classify(status) {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 429) return "rate_limited";
  if (status === 422 || status === 400) return "validation";
  if (status >= 500) return "server";
  return "error";
}

async function request(method, path, { body, query, signal } = {}) {
  let url = API_BASE + path;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v === undefined || v === null || v === "") continue;
      qs.set(k, v);
    }
    const s = qs.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }

  let res;
  try {
    res = await fetch(url, {
      method,
      credentials: "include", // send/receive the erm_session cookie cross-origin
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError("Could not reach the API at " + API_BASE + ". Is the backend running?", {
      kind: "network",
    });
  }

  let payload = null;
  const text = await res.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (err) {
      payload = text;
    }
  }

  if (!res.ok) {
    if (res.status === 401 && onUnauthorized) onUnauthorized();
    const detail =
      (payload && typeof payload === "object" && (payload.detail || payload.message)) ||
      (typeof payload === "string" ? payload : null) ||
      res.statusText ||
      "Request failed";
    throw new ApiError(typeof detail === "string" ? detail : JSON.stringify(detail), {
      status: res.status,
      kind: classify(res.status),
      body: payload,
    });
  }

  if (res.status === 204) return null;
  return payload;
}

export const api = {
  get: (path, opts) => request("GET", path, opts),
  post: (path, body, opts) => request("POST", path, { ...opts, body: body ?? {} }),
  patch: (path, body, opts) => request("PATCH", path, { ...opts, body: body ?? {} }),
  put: (path, body, opts) => request("PUT", path, { ...opts, body: body ?? {} }),
  delete: (path, opts) => request("DELETE", path, opts),
};

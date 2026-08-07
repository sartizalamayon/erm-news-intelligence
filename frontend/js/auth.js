// OTP-only auth (ADR §9.3, app/api/routers/auth.py + me.py — real,
// stable endpoints, unlike most of the rest of the API). The design
// prototype ships a password field + a login-time role picker; both are
// intentionally NOT built here — see the mission brief and
// frontend-contract.md §5/§8: role must come from the account
// (GET /me), never chosen client-side before a credential check.

import { api, ApiError } from "./api.js";

export function requestOtp(email) {
  return api.post("/auth/otp/request", { email });
}

export function verifyOtp(email, code) {
  return api.post("/auth/otp/verify", { email, code });
}

export function logout() {
  return api.post("/auth/logout", undefined).catch(() => null);
}

export function fetchMe() {
  return api.get("/me");
}

export { ApiError };

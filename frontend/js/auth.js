// Auth (AUTH-V2, app/api/routers/auth.py — real, live). OTP proves
// mailbox ownership ONCE (signup or password reset); every subsequent
// sign-in is email + password. No role-picker anywhere: role always
// comes from GET /me, never chosen client-side (frontend-contract.md
// §5/§8 flags the prototype's login-time role picker as a security
// defect).

import { api, ApiError } from "./api.js";

export function requestOtp(email, purpose = "signup") {
  return api.post("/auth/otp/request", { email, purpose });
}

// Consumes the OTP and sets the password in one call — same endpoint
// for a brand-new account's first password and an existing account's
// reset overwrite. Signs the caller in (session cookie) on success.
export function setPassword(email, code, newPassword) {
  return api.post("/auth/password/set", { email, code, new_password: newPassword });
}

export function login(email, password) {
  return api.post("/auth/login", { email, password });
}

// Already-authenticated user rotating their own password. Session
// cookie carries identity — current_password is required anyway (proof
// of intent even with a valid session). Backend revokes every OTHER
// session on success and keeps the current one.
export function changePassword(currentPassword, newPassword) {
  return api.post("/auth/password/change", { current_password: currentPassword, new_password: newPassword });
}

export function logout() {
  return api.post("/auth/logout", undefined).catch(() => null);
}

export function fetchMe() {
  return api.get("/me");
}

export { ApiError };

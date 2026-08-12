// Account / security panel (AUTH-V2) — change-password for the signed
// in user. Opened as a modal from the rail footer (shell.js). Not one
// of the design's 11 screens; the prototype has no equivalent, so this
// is built fresh, kept visually consistent with the rest of the app
// (card/input/button classes from css/app.css).

import { changePassword, ApiError } from "../auth.js";
import { state } from "../state.js";
import { escapeHtml } from "../util.js";

export function mountAccountPanel(root, { onClose }) {
  const s = { current: "", next: "", confirm: "", error: "", note: "", busy: false };

  render();

  function render() {
    const user = state.user || {};
    root.innerHTML = `
      <div class="account-modal-backdrop" id="accountBackdrop">
        <div class="card" style="max-width:420px;width:92vw;position:relative;" id="accountCard">
          <button id="accountCloseBtn" class="btn-ghost" style="position:absolute;top:14px;right:14px;font-size:16px;cursor:pointer;">&#10005;</button>
          <h3>Account & security</h3>
          <p class="hint">${escapeHtml(user.display_name || "")} &middot; ${escapeHtml(user.email || "")} &middot; ${escapeHtml(user.role || "")}</p>

          <label class="field-label">Current password</label>
          <input id="curPw" type="password" class="text-input" style="padding:10px 12px;font-size:13px;" autocomplete="current-password" value="${escapeHtml(s.current)}" />

          <label class="field-label" style="margin-top:14px;">New password</label>
          <input id="newPw" type="password" class="text-input" style="padding:10px 12px;font-size:13px;" autocomplete="new-password" value="${escapeHtml(s.next)}" />

          <label class="field-label" style="margin-top:14px;">Confirm new password</label>
          <input id="confirmPw" type="password" class="text-input" style="padding:10px 12px;font-size:13px;" autocomplete="new-password" value="${escapeHtml(s.confirm)}" />

          ${s.error ? `<div class="banner banner-error" style="margin-top:14px;"><span>&#10005;</span><div>${escapeHtml(s.error)}</div></div>` : ""}
          ${s.note ? `<div class="banner banner-ok" style="margin-top:14px;"><span>&#10003;</span><div>${escapeHtml(s.note)}</div></div>` : ""}

          <button id="changePwBtn" class="btn btn-primary" style="width:100%;margin-top:16px;padding:11px 18px;font-size:13.5px;" ${
            s.busy ? "disabled" : ""
          }>${s.busy ? "Saving…" : "Change password"}</button>
        </div>
      </div>
    `;
    bind();
  }

  function bind() {
    root.querySelector("#accountCloseBtn").addEventListener("click", onClose);
    root.querySelector("#accountBackdrop").addEventListener("click", (e) => {
      if (e.target.id === "accountBackdrop") onClose();
    });
    root.querySelector("#curPw").addEventListener("input", (e) => (s.current = e.target.value));
    root.querySelector("#newPw").addEventListener("input", (e) => (s.next = e.target.value));
    root.querySelector("#confirmPw").addEventListener("input", (e) => (s.confirm = e.target.value));
    root.querySelector("#changePwBtn").addEventListener("click", submit);
  }

  async function submit() {
    s.error = "";
    s.note = "";
    if (!s.current) {
      s.error = "Enter your current password.";
      render();
      return;
    }
    if (!s.next) {
      s.error = "Choose a new password.";
      render();
      return;
    }
    if (s.next !== s.confirm) {
      s.error = "New passwords don't match.";
      render();
      return;
    }
    s.busy = true;
    render();
    try {
      await changePassword(s.current, s.next);
      s.busy = false;
      s.current = s.next = s.confirm = "";
      s.note = "✓ Password changed. Your other sessions were signed out — this one stays signed in.";
      render();
    } catch (err) {
      s.busy = false;
      s.error = describeError(err);
      render();
    }
  }

  function describeError(err) {
    if (err instanceof ApiError) {
      // Wrong current password comes back as a 422 (app.errors.ValidationError
      // -> kind "validation" in api.js's classify()), not a 401 — the
      // session cookie itself is still valid, only the submitted password
      // is wrong. err.message already carries the backend's exact detail
      // text ("Current password is incorrect."), so this branch is a
      // belt-and-suspenders friendly fallback, not the primary path.
      if (err.kind === "validation") return err.message || "Current password is incorrect.";
      if (err.kind === "rate_limited") return err.message || "Too many attempts — wait a few minutes and try again.";
      if (err.message) return err.message;
    }
    return "Couldn't change your password. Try again.";
  }

  return { destroy() {} };
}

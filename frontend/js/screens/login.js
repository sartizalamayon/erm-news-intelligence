// Login screen — OTP only (ADR §9.3). Two round trips: request a code,
// then verify it. Recreates the prototype's visual language (logo,
// card, domain-suffix chip) but replaces the password field + role
// picker with an OTP code step and no role selection at all — role
// comes from GET /me after verify (frontend-contract.md §5/§8 flags the
// prototype's role-picker as a security defect, not a requirement).

import { requestOtp, verifyOtp, fetchMe, ApiError } from "../auth.js";
import { getTheme, applyTheme, themeGlyph } from "../theme.js";
import { escapeHtml } from "../util.js";
import { setState } from "../state.js";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function mountLogin(root, { onAuthenticated }) {
  const local = {
    step: "email", // "email" | "code"
    email: "",
    code: "",
    error: "",
    busy: false,
    resendCooldown: 0,
  };
  let cooldownTimer = null;

  function render() {
    const theme = getTheme();
    root.innerHTML = `
      <div class="login-screen">
        <button class="icon-btn" id="loginTheme" title="Switch theme" style="position:absolute;top:22px;right:24px;">${themeGlyph(theme)}</button>
        <div class="login-box">
          <div class="login-logo-wrap">
            <div class="login-logo">ERM</div>
            <div class="login-kicker">Internal · restricted</div>
          </div>
          <div class="login-card">
            ${local.step === "email" ? renderEmailStep() : renderCodeStep()}
            ${local.error ? `<div class="banner banner-error" style="margin-top:16px;"><span>&#10005;</span><div>${escapeHtml(local.error)}</div></div>` : ""}
            <button id="loginSubmit" class="btn btn-primary" style="width:100%;margin-top:22px;padding:13px 20px;font-size:14.5px;" ${
              local.busy ? "disabled" : ""
            }>${local.busy ? "Please wait…" : local.step === "email" ? "Send code" : "Verify & sign in"}</button>
            ${
              local.step === "code"
                ? `<div class="login-links">
                    <button id="loginChangeEmail" class="btn-ghost" style="cursor:pointer;font-size:12px;">&larr; Change email</button>
                    <button id="loginResend" class="btn-ghost" style="cursor:pointer;font-size:12px;" ${
                      local.resendCooldown > 0 ? "disabled" : ""
                    }>${local.resendCooldown > 0 ? `Resend in ${local.resendCooldown}s` : "Resend code"}</button>
                  </div>`
                : ""
            }
          </div>
          <p class="login-footnote">Authorised accounts only. Activity is logged.</p>
        </div>
      </div>
    `;
    bind();
  }

  function renderEmailStep() {
    return `
      <label class="field-label">Work email</label>
      <input id="loginEmail" type="email" class="text-input" placeholder="firstname.lastname@grameenphone.com"
        autocomplete="username" spellcheck="false" value="${escapeHtml(local.email)}" />
    `;
  }

  function renderCodeStep() {
    return `
      <div style="font-size:13px;color:var(--tx2);line-height:1.5;margin-bottom:16px;">
        A 6-digit code was sent to <b style="color:var(--tx);">${escapeHtml(local.email)}</b>. It expires in 10 minutes.
      </div>
      <label class="field-label">Verification code</label>
      <input id="loginCode" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="6" class="otp-input"
        placeholder="000000" value="${escapeHtml(local.code)}" />
    `;
  }

  function bind() {
    const themeBtn = root.querySelector("#loginTheme");
    themeBtn.addEventListener("click", () => {
      const next = getTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      setState({ theme: next });
      render();
    });

    const submit = root.querySelector("#loginSubmit");
    submit.addEventListener("click", local.step === "email" ? handleSendCode : handleVerify);

    if (local.step === "email") {
      const emailInput = root.querySelector("#loginEmail");
      emailInput.focus();
      emailInput.addEventListener("input", (e) => {
        local.email = e.target.value;
      });
      emailInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleSendCode();
      });
    } else {
      const codeInput = root.querySelector("#loginCode");
      codeInput.focus();
      codeInput.addEventListener("input", (e) => {
        local.code = e.target.value.replace(/\D/g, "").slice(0, 6);
        e.target.value = local.code;
      });
      codeInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleVerify();
      });
      root.querySelector("#loginChangeEmail").addEventListener("click", () => {
        local.step = "email";
        local.code = "";
        local.error = "";
        render();
      });
      const resendBtn = root.querySelector("#loginResend");
      resendBtn.addEventListener("click", () => {
        if (local.resendCooldown > 0) return;
        handleSendCode(true);
      });
    }
  }

  function startCooldown() {
    local.resendCooldown = 30;
    clearInterval(cooldownTimer);
    cooldownTimer = setInterval(() => {
      local.resendCooldown -= 1;
      if (local.resendCooldown <= 0) {
        clearInterval(cooldownTimer);
        local.resendCooldown = 0;
      }
      // Cheap partial update: only touch the resend button's text so we
      // don't steal focus from the code input every second.
      const btn = root.querySelector("#loginResend");
      if (btn) {
        btn.disabled = local.resendCooldown > 0;
        btn.textContent = local.resendCooldown > 0 ? `Resend in ${local.resendCooldown}s` : "Resend code";
      } else {
        clearInterval(cooldownTimer);
      }
    }, 1000);
  }

  async function handleSendCode(isResend = false) {
    const raw = local.email.trim().toLowerCase();
    if (!raw) {
      local.error = "Enter your work email address.";
      render();
      return;
    }
    if (!EMAIL_RE.test(raw)) {
      local.error = "That doesn't look like a valid email address.";
      render();
      return;
    }
    local.email = raw;
    local.busy = true;
    local.error = "";
    render();
    try {
      await requestOtp(raw);
      local.busy = false;
      local.step = "code";
      render();
      startCooldown();
    } catch (err) {
      local.busy = false;
      local.error = friendlyError(err, isResend ? "Could not resend the code." : "Could not send the code.");
      render();
    }
  }

  async function handleVerify() {
    if (local.code.length !== 6) {
      local.error = "Enter the 6-digit code.";
      render();
      return;
    }
    local.busy = true;
    local.error = "";
    render();
    try {
      await verifyOtp(local.email, local.code);
      const me = await fetchMe();
      clearInterval(cooldownTimer);
      onAuthenticated(me);
    } catch (err) {
      local.busy = false;
      local.error = friendlyError(err, "That code didn't work.");
      render();
    }
  }

  function friendlyError(err, fallback) {
    if (err instanceof ApiError) {
      if (err.kind === "rate_limited") return err.message || "Too many attempts — wait a moment and try again.";
      if (err.kind === "network") return err.message;
      if (err.message) return err.message;
    }
    return fallback;
  }

  render();

  return {
    destroy() {
      clearInterval(cooldownTimer);
    },
  };
}

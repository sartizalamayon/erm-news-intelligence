// Login (AUTH-V2). Default path is email + password. Two side routes —
// "set up your account" (signup) and "forgot password" (reset) — both
// prove mailbox ownership with a one-time code, then set a password via
// the same POST /auth/password/set call. No role-picker: role always
// comes from GET /me after the server confirms identity.

import { login, requestOtp, setPassword, ApiError } from "../auth.js";
import { getTheme, applyTheme, themeGlyph } from "../theme.js";
import { escapeHtml } from "../util.js";
import { setState } from "../state.js";

const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

export function mountLogin(root, { onAuthenticated }) {
  const local = {
    step: "login", // login | otp-email | otp-code
    purpose: "signup", // "signup" | "password_reset", which otp-email/otp-code flow we're in
    email: "",
    password: "",
    code: "",
    newPassword: "",
    confirmPassword: "",
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
            ${local.step === "login" ? renderLoginStep() : local.step === "otp-email" ? renderOtpEmailStep() : renderOtpCodeStep()}
            ${local.error ? `<div class="banner banner-error" style="margin-top:16px;"><span>&#10005;</span><div>${escapeHtml(local.error)}</div></div>` : ""}
            <button id="loginSubmit" class="btn btn-primary" style="width:100%;margin-top:22px;padding:13px 20px;font-size:14.5px;" ${
              local.busy ? "disabled" : ""
            }>${submitLabel()}</button>
            ${renderFooterLinks()}
          </div>
          <p class="login-footnote">Authorised accounts only. Activity is logged.</p>
        </div>
      </div>
    `;
    bind();
  }

  function submitLabel() {
    if (local.busy) return "Please wait…";
    if (local.step === "login") return "Sign in";
    if (local.step === "otp-email") return "Send code";
    return local.purpose === "signup" ? "Create account" : "Reset password";
  }

  function renderLoginStep() {
    return `
      <label class="field-label">Work email</label>
      <input id="loginEmail" type="email" class="text-input" placeholder="firstname.lastname@grameenphone.com"
        autocomplete="username" spellcheck="false" value="${escapeHtml(local.email)}" />
      <label class="field-label" style="margin-top:18px;">Password</label>
      <input id="loginPassword" type="password" class="text-input" placeholder="••••••••" autocomplete="current-password" value="${escapeHtml(
        local.password
      )}" />
    `;
  }

  function renderOtpEmailStep() {
    return `
      <div style="font-size:13px;color:var(--tx2);line-height:1.5;margin-bottom:14px;">
        ${local.purpose === "signup" ? "First time here? Verify your work email to set up your account." : "Enter your work email and we'll send a code to reset your password."}
      </div>
      <label class="field-label">Work email</label>
      <input id="loginEmail" type="email" class="text-input" placeholder="firstname.lastname@grameenphone.com"
        autocomplete="username" spellcheck="false" value="${escapeHtml(local.email)}" />
    `;
  }

  function renderOtpCodeStep() {
    return `
      <div style="font-size:13px;color:var(--tx2);line-height:1.5;margin-bottom:12px;">
        A 6-digit code was sent to <b style="color:var(--tx);">${escapeHtml(local.email)}</b>. It expires in 10 minutes.
      </div>
      <div class="banner banner-info" style="margin:0 0 16px;">
        <span>&#9888;</span>
        <div><b>Check your Junk folder.</b> Codes currently land there, not your inbox. Open it and mark the message <b>Not Junk</b> so future codes arrive normally.</div>
      </div>
      <label class="field-label">Verification code</label>
      <input id="loginCode" type="text" inputmode="numeric" pattern="[0-9]*" maxlength="6" class="otp-input"
        placeholder="000000" value="${escapeHtml(local.code)}" />
      <label class="field-label" style="margin-top:18px;">${local.purpose === "signup" ? "Choose a password" : "New password"}</label>
      <input id="loginNewPassword" type="password" class="text-input" placeholder="••••••••" autocomplete="new-password" value="${escapeHtml(
        local.newPassword
      )}" />
      <label class="field-label" style="margin-top:18px;">Confirm password</label>
      <input id="loginConfirmPassword" type="password" class="text-input" placeholder="••••••••" autocomplete="new-password" value="${escapeHtml(
        local.confirmPassword
      )}" />
    `;
  }

  function renderFooterLinks() {
    if (local.step === "login") {
      return `<div class="login-links">
        <button id="gotoSignup" class="btn-ghost" style="cursor:pointer;font-size:12px;">First time here? Set up your account</button>
        <button id="gotoReset" class="btn-ghost" style="cursor:pointer;font-size:12px;">Forgot password?</button>
      </div>`;
    }
    if (local.step === "otp-email") {
      return `<div class="login-links" style="justify-content:center;"><button id="backToLogin" class="btn-ghost" style="cursor:pointer;font-size:12px;">&larr; Back to sign in</button></div>`;
    }
    return `<div class="login-links">
      <button id="changeEmail" class="btn-ghost" style="cursor:pointer;font-size:12px;">&larr; Change email</button>
      <button id="resendCode" class="btn-ghost" style="cursor:pointer;font-size:12px;" ${
        local.resendCooldown > 0 ? "disabled" : ""
      }>${local.resendCooldown > 0 ? `Resend in ${local.resendCooldown}s` : "Resend code"}</button>
    </div>`;
  }

  function bind() {
    root.querySelector("#loginTheme").addEventListener("click", () => {
      const next = getTheme() === "dark" ? "light" : "dark";
      applyTheme(next);
      setState({ theme: next });
      render();
    });

    root.querySelector("#loginSubmit").addEventListener("click", handleSubmit);

    const emailInput = root.querySelector("#loginEmail");
    if (emailInput) {
      emailInput.addEventListener("input", (e) => (local.email = e.target.value));
      emailInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleSubmit();
      });
      if (local.step === "login" || !local.password) emailInput.focus();
    }
    const passwordInput = root.querySelector("#loginPassword");
    if (passwordInput) {
      passwordInput.addEventListener("input", (e) => (local.password = e.target.value));
      passwordInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleSubmit();
      });
    }
    const codeInput = root.querySelector("#loginCode");
    if (codeInput) {
      codeInput.focus();
      codeInput.addEventListener("input", (e) => {
        local.code = e.target.value.replace(/\D/g, "").slice(0, 6);
        e.target.value = local.code;
      });
    }
    const newPw = root.querySelector("#loginNewPassword");
    if (newPw) newPw.addEventListener("input", (e) => (local.newPassword = e.target.value));
    const confirmPw = root.querySelector("#loginConfirmPassword");
    if (confirmPw) {
      confirmPw.addEventListener("input", (e) => (local.confirmPassword = e.target.value));
      confirmPw.addEventListener("keydown", (e) => {
        if (e.key === "Enter") handleSubmit();
      });
    }

    const gotoSignup = root.querySelector("#gotoSignup");
    if (gotoSignup) gotoSignup.addEventListener("click", () => switchTo("otp-email", "signup"));
    const gotoReset = root.querySelector("#gotoReset");
    if (gotoReset) gotoReset.addEventListener("click", () => switchTo("otp-email", "password_reset"));
    const backToLogin = root.querySelector("#backToLogin");
    if (backToLogin) backToLogin.addEventListener("click", () => switchTo("login"));
    const changeEmail = root.querySelector("#changeEmail");
    if (changeEmail) changeEmail.addEventListener("click", () => switchTo("otp-email", local.purpose));
    const resendCode = root.querySelector("#resendCode");
    if (resendCode) resendCode.addEventListener("click", () => handleRequestOtp(true));
  }

  function switchTo(step, purpose) {
    local.step = step;
    if (purpose) local.purpose = purpose;
    local.error = "";
    local.code = "";
    local.newPassword = "";
    local.confirmPassword = "";
    render();
  }

  function handleSubmit() {
    if (local.step === "login") return handleLogin();
    if (local.step === "otp-email") return handleRequestOtp(false);
    return handleSetPassword();
  }

  async function handleLogin() {
    const raw = local.email.trim().toLowerCase();
    if (!raw || !EMAIL_RE.test(raw)) {
      local.error = "Enter a valid work email address.";
      render();
      return;
    }
    if (!local.password) {
      local.error = "Enter your password.";
      render();
      return;
    }
    local.busy = true;
    local.error = "";
    render();
    try {
      const res = await login(raw, local.password);
      onAuthenticated(res);
    } catch (err) {
      local.busy = false;
      // 422 from /auth/login is deliberately generic (no user-enumeration) — never elaborate it.
      local.error = describeAuthError(err, "Incorrect email or password.");
      render();
    }
  }

  async function handleRequestOtp(isResend) {
    const raw = local.email.trim().toLowerCase();
    if (!raw || !EMAIL_RE.test(raw)) {
      local.error = "Enter a valid work email address.";
      render();
      return;
    }
    local.email = raw;
    local.busy = true;
    local.error = "";
    render();
    try {
      await requestOtp(raw, local.purpose);
      local.busy = false;
      local.step = "otp-code";
      render();
      startCooldown();
    } catch (err) {
      local.busy = false;
      local.error = describeAuthError(err, isResend ? "Could not resend the code." : "Could not send the code.");
      render();
    }
  }

  async function handleSetPassword() {
    if (local.code.length !== 6) {
      local.error = "Enter the 6-digit code.";
      render();
      return;
    }
    if (!local.newPassword) {
      local.error = "Choose a password.";
      render();
      return;
    }
    if (local.newPassword !== local.confirmPassword) {
      local.error = "Passwords don't match.";
      render();
      return;
    }
    local.busy = true;
    local.error = "";
    render();
    try {
      const res = await setPassword(local.email, local.code, local.newPassword);
      clearInterval(cooldownTimer);
      onAuthenticated(res);
    } catch (err) {
      local.busy = false;
      local.error = describeAuthError(err, "That code didn't work.");
      render();
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
      const btn = root.querySelector("#resendCode");
      if (btn) {
        btn.disabled = local.resendCooldown > 0;
        btn.textContent = local.resendCooldown > 0 ? `Resend in ${local.resendCooldown}s` : "Resend code";
      } else {
        clearInterval(cooldownTimer);
      }
    }, 1000);
  }

  // Never leak *why* auth failed beyond what the server chose to say.
  // 429s and email-delivery failures (502/503 from the OTP send) get
  // clear, specific copy; everything else falls back to the server's
  // own (deliberately generic, for /auth/login) message.
  function describeAuthError(err, fallback) {
    if (err instanceof ApiError) {
      if (err.kind === "rate_limited") return err.message || "Too many attempts. Wait a few minutes and try again.";
      if (err.kind === "network") return err.message;
      if (err.kind === "server") return "We couldn't send that email right now. The mail service is temporarily unavailable. Try again shortly.";
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

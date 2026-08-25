// Account / security panel (AUTH-V2) — change-password for the signed
// in user. Opened as a modal from the rail footer (shell.js). Not one
// of the design's 11 screens; the prototype has no equivalent, so this
// is built fresh, kept visually consistent with the rest of the app
// (card/input/button classes from css/app.css).
//
// IT IS A MODAL DIALOG, AND IT NOW SAYS SO.
//
// This rendered as a plain `<div>` over a dimmed backdrop: no
// `role="dialog"`, no `aria-modal`, no initial focus, no Escape, and no
// focus trap. Visually it was a modal; to the keyboard and to a screen
// reader it was three password boxes appended to the end of the page.
// What that meant in practice:
//
//   * Opening it left focus on the rail button behind the backdrop, so
//     a keyboard user had to tab forward through the whole dialog to
//     reach the first field, without being told a dialog had opened.
//   * Tab walked straight out of the last control and into the page
//     underneath — the sidebar links, the screen's own buttons — all of
//     which are visually behind a 55% black overlay and cannot be seen
//     while they are focused. The user is then typing into a form they
//     cannot see, and the only way back is to keep tabbing.
//   * Escape did nothing. Every other overlay convention on the web
//     closes on Escape; the only exits here were the × and a click on
//     the backdrop, neither reachable without a mouse or a long tab.
//
// A screen for CHANGING A PASSWORD is the worst possible place for
// focus to wander somewhere invisible, because the thing being typed is
// masked: there is no way to see where the characters are going.
//
// The trap below is deliberately hand-rolled and small rather than a
// dependency, matching the rest of this frontend, and it is built on
// the modal's own DOM so it cannot fall out of step with what renders.

import { changePassword, ApiError } from "../auth.js";
import { state } from "../state.js";
import { escapeHtml } from "../util.js";

/**
 * Everything inside the dialog that can take focus, in DOM order.
 *
 * Queried fresh on each Tab rather than cached at mount, because this
 * component re-renders itself (a validation error adds a banner, a
 * successful save swaps the button label) and a cached list would trap
 * focus against nodes that no longer exist.
 */
function focusables(card) {
  return [...card.querySelectorAll("button, input, select, textarea, a[href]")].filter(
    (el) => !el.disabled && el.tabIndex !== -1 && el.offsetParent !== null
  );
}

export function mountAccountPanel(root, { onClose }) {
  const s = { current: "", next: "", confirm: "", error: "", note: "", busy: false };
  let firstRender = true;
  // Where focus was when the dialog opened, so it can be put back on
  // close. Without this, closing the dialog drops focus onto <body> and
  // a keyboard user restarts their tab journey from the top of the page
  // rather than from the control they opened this with.
  const opener = typeof document !== "undefined" ? document.activeElement : null;

  function onKeyDown(e) {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key !== "Tab") return;
    const card = root.querySelector("#accountCard");
    if (!card) return;
    const items = focusables(card);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    // `document.activeElement` rather than `e.target`: focus may be on
    // the card itself or have escaped to <body> after a re-render, and
    // in both cases the right answer is to pull it back to an end of
    // the list rather than to let the browser's own order take over.
    const here = document.activeElement;
    if (!card.contains(here)) {
      e.preventDefault();
      (e.shiftKey ? last : first).focus();
      return;
    }
    if (e.shiftKey && here === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && here === last) {
      e.preventDefault();
      first.focus();
    }
  }

  // Attached once, at the document, and removed in destroy(). At the
  // document because a keystroke has to be caught wherever focus
  // currently is, including the moment after a re-render when it is
  // briefly on <body>; once, because bind() runs on every render and a
  // listener added there would stack up one copy per keystroke handled.
  if (typeof document !== "undefined") document.addEventListener("keydown", onKeyDown, true);

  render();

  function render() {
    const user = state.user || {};
    // Which control had focus before the DOM under it is thrown away.
    // A re-render here is always the result of a submit, so without
    // this the person who just pressed "Change password" and got a
    // validation error is left with focus on <body>, outside the trap,
    // with no announcement of the error.
    const hadFocus = document.activeElement && document.activeElement.id;
    root.innerHTML = `
      <div class="account-modal-backdrop" id="accountBackdrop">
        <div class="card" style="max-width:420px;width:92vw;position:relative;" id="accountCard"
             role="dialog" aria-modal="true" aria-labelledby="accountTitle">
          <button id="accountCloseBtn" class="btn-ghost" aria-label="Close account and security"
                  style="position:absolute;top:14px;right:14px;font-size:16px;cursor:pointer;">&#10005;</button>
          <h3 id="accountTitle">Account & security</h3>
          <p class="hint">${escapeHtml(user.display_name || "")} &middot; ${escapeHtml(user.email || "")} &middot; ${escapeHtml(user.role || "")}</p>

          <label class="field-label" for="curPw">Current password</label>
          <input id="curPw" type="password" class="text-input" style="padding:10px 12px;font-size:13px;" autocomplete="current-password" value="${escapeHtml(s.current)}" />

          <label class="field-label" for="newPw" style="margin-top:14px;">New password</label>
          <input id="newPw" type="password" class="text-input" style="padding:10px 12px;font-size:13px;" autocomplete="new-password" value="${escapeHtml(s.next)}" />

          <label class="field-label" for="confirmPw" style="margin-top:14px;">Confirm new password</label>
          <input id="confirmPw" type="password" class="text-input" style="padding:10px 12px;font-size:13px;" autocomplete="new-password" value="${escapeHtml(s.confirm)}" />

          ${s.error ? `<div class="banner banner-error" role="alert" style="margin-top:14px;"><span>&#10005;</span><div>${escapeHtml(s.error)}</div></div>` : ""}
          ${s.note ? `<div class="banner banner-ok" role="status" style="margin-top:14px;"><span>&#10003;</span><div>${escapeHtml(s.note)}</div></div>` : ""}

          <button id="changePwBtn" class="btn btn-primary" style="width:100%;margin-top:16px;padding:11px 18px;font-size:13.5px;" ${
            s.busy ? "disabled" : ""
          }>${s.busy ? "Saving…" : "Change password"}</button>
        </div>
      </div>
    `;
    bind();

    if (firstRender) {
      firstRender = false;
      // The first field, not the close button: the dialog exists to be
      // filled in, and landing on × invites closing it by reflex.
      root.querySelector("#curPw")?.focus();
      return;
    }
    const back = hadFocus && root.querySelector(`#${CSS.escape(hadFocus)}`);
    (back || root.querySelector("#curPw") || root.querySelector("#accountCloseBtn"))?.focus();
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
      s.note = "✓ Password changed. Your other sessions were signed out. This one stays signed in.";
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
      if (err.kind === "rate_limited") return err.message || "Too many attempts. Wait a few minutes and try again.";
      if (err.message) return err.message;
    }
    return "Couldn't change your password. Try again.";
  }

  return {
    destroy() {
      if (typeof document !== "undefined") document.removeEventListener("keydown", onKeyDown, true);
      // Only when focus is still in the dialog, or has already fallen
      // to nothing in particular (clicking the backdrop lands it on
      // <body>). If some other control has legitimately taken it since
      // — a sign-out, a navigation — yanking it back to a button that
      // may no longer be on screen is worse than leaving it alone.
      const here = document.activeElement;
      const adrift = !here || here === document.body || root.contains(here);
      if (opener && typeof opener.focus === "function" && adrift) opener.focus();
    },
  };
}

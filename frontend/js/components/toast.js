// A result toast: the answer to "I clicked test — what happened?"
//
// This exists because the same banner was written twice, by hand, in
// health.js and sources.js — and the two copies drifted. health.js's
// was fixed to be viewport-pinned after it turned out that testing a
// source below the fold rendered its result ~1,554px off-screen, making
// a working button look dead. sources.js kept the original inline
// version and therefore kept the bug, on a page that also lists 51
// sources with a test button on every row.
//
// That is the argument for a component layer in one paragraph: not
// abstraction for its own sake, but so a fix lands once instead of
// having to be remembered in every copy.
//
// Deliberately a STRING BUILDER, not a mounted widget. Every screen here
// renders by assigning template strings to innerHTML; a component that
// required its own lifecycle would fight that. It returns markup and
// takes an id for the dismiss button, so the caller keeps ownership of
// its own event wiring.

import { escapeHtml } from "../util.js";

/**
 * @param {object} opts
 * @param {string|null} opts.message  Text to show. Falsy renders nothing.
 * @param {boolean} [opts.ok]         Success styling when true, error when false.
 * @param {string}  [opts.dismissId]  DOM id for the dismiss button.
 * @returns {string} HTML, or "" when there is nothing to say.
 */
export function resultToast({ message, ok = true, dismissId = "toastDismissBtn" }) {
  if (!message) return "";
  // Pinned to the VIEWPORT, not placed in document flow. Both screens
  // that use this render it above a long table, so an in-flow banner is
  // invisible for exactly the rows a user is most likely to be testing.
  // `scrollIntoView` was tried first and is unreliable here: the scroll
  // container is `main.main-area`, not the document.
  //
  // role/aria-live so the result is announced rather than only seen.
  return `<div class="banner ${ok ? "banner-ok" : "banner-error"}" role="status" aria-live="polite"
       style="position:fixed;left:50%;transform:translateX(-50%);bottom:24px;z-index:60;
              max-width:min(760px,92vw);box-shadow:0 8px 28px rgba(0,0,0,.28);">
    <span>${ok ? "&#10003;" : "&#10005;"}</span>
    <div style="flex:1;">${escapeHtml(message)}</div>
    <button id="${escapeHtml(dismissId)}" class="btn-ghost" style="font-size:14px;"
            aria-label="Dismiss">&#10005;</button>
  </div>`;
}

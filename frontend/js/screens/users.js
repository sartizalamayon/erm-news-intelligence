// User management: list accounts, create one directly (bypassing OTP
// self-signup), change a role, deactivate/reactivate, delete. Follows
// the same closure/state/render/bind convention as models.js and
// sources.js, and reuses sources.js's table/card/banner markup, since
// this screen is the same shape: an admin-managed table of rows with
// per-row actions and an "add" form.
//
// Endpoints (app/api/routers/users.py):
//   GET    /users
//   POST   /users                 { email, password, role }
//   PATCH  /users/{id}/role       { role }
//   PATCH  /users/{id}/active     { is_active }
//   DELETE /users/{id}
//
// An admin cannot change their own role, active state, or delete their
// own account here — the server rejects all three (400) as a guard
// against locking yourself out of your own session — so those three
// actions are disabled client-side too for the row matching the
// signed-in account, with a tooltip saying why.

import { api } from "../api.js";
import { state } from "../state.js";
import { escapeHtml, fmtErr, fmtTimestamp, notWiredBanner, skeletonRows } from "../util.js";

const ROLES = ["admin", "analyst"];

export function mountUsers(root) {
  let alive = true;
  const s = {
    users: [],
    loading: true,
    error: null,
    creating: false,
    busyIds: new Set(),
    note: "",
    noteOk: true,
  };

  render();
  load();

  async function load() {
    s.loading = true;
    render();
    try {
      s.users = await api.get("/users");
      s.error = null;
    } catch (err) {
      s.error = err;
    }
    s.loading = false;
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

  function isSelf(u) {
    return !!(state.user && state.user.email && u.email === state.user.email);
  }

  /** Disabled-attr + tooltip for the three self-guarded controls. */
  function selfGuard(u) {
    return isSelf(u) ? `disabled title="You can't change your own account here"` : "";
  }

  // ── actions ──────────────────────────────────────────────────────────

  async function createUser() {
    const emailEl = root.querySelector("#newUserEmail");
    const passwordEl = root.querySelector("#newUserPassword");
    const roleEl = root.querySelector("#newUserRole");
    const errEl = root.querySelector("#newUserError");
    const showError = (msg) => {
      if (!errEl) return;
      errEl.textContent = msg;
      errEl.style.display = "";
    };
    if (errEl) errEl.style.display = "none";

    const email = emailEl.value.trim();
    const password = passwordEl.value;
    const role = roleEl.value;
    if (!email) return showError("Email is required.");
    if (!password) return showError("Password is required.");

    const saveBtn = root.querySelector("#newUserSave");
    if (saveBtn) saveBtn.disabled = true;
    try {
      await api.post("/users", { email, password, role });
      s.creating = false;
      await load();
      flash("User created.", true);
    } catch (err) {
      showError("Couldn't create user: " + fmtErr(err));
    } finally {
      if (saveBtn) saveBtn.disabled = false;
    }
  }

  async function changeRole(u, role) {
    if (isSelf(u) || s.busyIds.has(u.id)) return;
    s.busyIds.add(u.id);
    render();
    try {
      await api.patch(`/users/${u.id}/role`, { role });
      await load();
      flash(`${u.email} is now ${role}.`, true);
    } catch (err) {
      flash("Couldn't change role: " + fmtErr(err), false);
    } finally {
      s.busyIds.delete(u.id);
      render();
    }
  }

  async function toggleActive(u) {
    if (isSelf(u) || s.busyIds.has(u.id)) return;
    const next = !u.is_active;
    if (next === false && !window.confirm(`Deactivate ${u.email}? They will be signed out immediately.`)) return;
    s.busyIds.add(u.id);
    render();
    try {
      await api.patch(`/users/${u.id}/active`, { is_active: next });
      await load();
      flash(next ? `${u.email} reactivated.` : `${u.email} deactivated.`, true);
    } catch (err) {
      flash("Couldn't update account: " + fmtErr(err), false);
    } finally {
      s.busyIds.delete(u.id);
      render();
    }
  }

  async function deleteUser(u) {
    if (!u || isSelf(u)) return;
    if (!window.confirm(`Delete ${u.email}? This cannot be undone.`)) return;
    try {
      await api.delete(`/users/${u.id}`);
      await load();
      flash(`Deleted ${u.email}.`, true);
    } catch (err) {
      flash(`Couldn't delete ${u.email}: ` + fmtErr(err), false);
    }
  }

  // ── fragments ────────────────────────────────────────────────────────

  function userRow(u) {
    const self = isSelf(u);
    const guard = selfGuard(u);
    const busy = s.busyIds.has(u.id);
    return `<div class="table-row users-cols" style="opacity:${u.is_active ? 1 : 0.55};">
      <div style="min-width:0;">
        <div class="row" style="gap:7px;">
          <span style="font-size:13.5px;font-weight:600;letter-spacing:-.1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(u.email)}</span>
          ${self ? `<span class="pill" style="background:var(--acsoft);color:var(--actx);">you</span>` : ""}
        </div>
        <div class="row" style="gap:8px;margin-top:4px;">
          <span class="pill" style="background:${u.is_active ? "var(--acsoft)" : "var(--gridline)"};color:${u.is_active ? "var(--actx)" : "var(--tx3)"};"><span class="pill-dot" style="background:${u.is_active ? "var(--ac2)" : "var(--tx4)"}"></span>${u.is_active ? "active" : "deactivated"}</span>
          <span class="mono" style="font-size:10px;color:var(--tx4);">${u.has_password ? "password set" : "no password yet"}</span>
        </div>
      </div>
      <div>
        <select class="text-input" style="padding:7px 9px;font-size:12.5px;" data-role-select="${u.id}" ${guard} ${busy ? "disabled" : ""}>
          ${ROLES.map((r) => `<option value="${r}" ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}
        </select>
      </div>
      <div class="mono" style="font-size:11.5px;color:var(--tx2);">${u.last_login_at ? escapeHtml(fmtTimestamp(u.last_login_at)) : "never"}</div>
      <div class="row" style="gap:6px;justify-content:flex-end;">
        <button class="btn-sm ${u.is_active ? "btn-sm-danger" : ""}" data-toggle-active="${u.id}" ${guard} ${busy ? "disabled" : ""}>${
          busy ? "…" : u.is_active ? "deactivate" : "reactivate"
        }</button>
        <button class="btn-sm btn-sm-danger" data-delete="${u.id}" ${guard}>delete</button>
      </div>
    </div>`;
  }

  function createForm() {
    return `<div class="card" style="margin-bottom:16px;">
      <h3>Add user</h3>
      <div class="stack" style="gap:10px;max-width:420px;">
        <div>
          <label class="field-label" for="newUserEmail">Work email</label>
          <input id="newUserEmail" class="text-input" type="email" style="padding:9px 12px;font-size:13px;" placeholder="firstname.lastname@grameenphone.com" />
        </div>
        <div>
          <label class="field-label" for="newUserPassword">Password</label>
          <input id="newUserPassword" class="text-input" type="password" style="padding:9px 12px;font-size:13px;" placeholder="at least 12 characters" />
        </div>
        <div>
          <label class="field-label" for="newUserRole">Role</label>
          <select id="newUserRole" class="text-input" style="padding:9px 12px;font-size:13px;">
            ${ROLES.map((r) => `<option value="${r}">${r}</option>`).join("")}
          </select>
        </div>
        <div id="newUserError" role="alert" aria-live="assertive"
             style="display:none;font-size:12px;color:var(--red);background:color-mix(in oklab, var(--red) 12%, transparent);border:1px solid var(--red);border-radius:6px;padding:8px 11px;"></div>
        <div class="row" style="gap:9px;">
          <button id="newUserSave" class="btn btn-primary" style="padding:9px 16px;">Create user</button>
          <button id="newUserCancel" class="btn btn-outline" style="padding:9px 16px;">Cancel</button>
        </div>
      </div>
    </div>`;
  }

  // ── render ───────────────────────────────────────────────────────────

  function render() {
    root.innerHTML = `<div class="page page-wide">
      <div class="row-between wrap" style="margin-bottom:20px;">
        <p style="color:var(--tx2);font-size:13.5px;line-height:1.6;margin:0;max-width:60ch;">
          Create and manage accounts directly. Set a password of at least 12 characters
        </p>
        <button id="addUserBtn" class="btn btn-primary" style="padding:11px 18px;white-space:nowrap;">+ Add user</button>
      </div>

      ${s.creating ? createForm() : ""}

      ${
        s.note
          ? `<div class="banner ${s.noteOk ? "banner-ok" : "banner-error"}"><span>${s.noteOk ? "&#10003;" : "&#10005;"}</span><div style="flex:1;">${escapeHtml(s.note)}</div></div>`
          : ""
      }

      ${
        s.error
          ? s.error.kind === "forbidden"
            ? `<div class="banner banner-error"><span>&#128274;</span><div>User management is admin only. Your account doesn't have the admin role.</div></div>`
            : s.error.kind === "not_found"
            ? notWiredBanner("GET /users")
            : `<div class="banner banner-error"><span>&#10005;</span><div style="flex:1;">${escapeHtml(fmtErr(s.error))}</div><button id="usersRetry" class="btn-ghost" style="color:var(--red);text-decoration:underline;">Retry</button></div>`
          : `<div class="table-wrap">
            <div class="table-head-row users-cols">
              <div class="table-th">Email</div><div class="table-th">Role</div><div class="table-th">Last login</div><div class="table-th" style="text-align:right;">Actions</div>
            </div>
            ${
              s.loading
                ? Array.from({ length: 4 })
                    .map(() => `<div class="table-row users-cols">${skeletonRows(1).repeat(4)}</div>`)
                    .join("")
                : s.users.length
                ? s.users.map(userRow).join("")
                : `<div class="state-block"><div class="state-title">No users yet</div><p class="state-body">Add the first account to get started.</p></div>`
            }
          </div>`
      }
    </div>`;

    bind();
  }

  // ── binding ──────────────────────────────────────────────────────────

  function bind() {
    const addBtn = root.querySelector("#addUserBtn");
    if (addBtn) addBtn.addEventListener("click", () => { s.creating = true; render(); });

    const retry = root.querySelector("#usersRetry");
    if (retry) retry.addEventListener("click", () => load());

    const saveBtn = root.querySelector("#newUserSave");
    if (saveBtn) saveBtn.addEventListener("click", createUser);

    const cancelBtn = root.querySelector("#newUserCancel");
    if (cancelBtn) cancelBtn.addEventListener("click", () => { s.creating = false; render(); });

    root.querySelectorAll("[data-role-select]").forEach((sel) =>
      sel.addEventListener("change", (e) => {
        const id = Number(sel.getAttribute("data-role-select"));
        const u = s.users.find((x) => x.id === id);
        if (u) changeRole(u, e.target.value);
      })
    );
    root.querySelectorAll("[data-toggle-active]").forEach((b) =>
      b.addEventListener("click", () => {
        const id = Number(b.getAttribute("data-toggle-active"));
        const u = s.users.find((x) => x.id === id);
        if (u) toggleActive(u);
      })
    );
    root.querySelectorAll("[data-delete]").forEach((b) =>
      b.addEventListener("click", () => {
        const id = Number(b.getAttribute("data-delete"));
        const u = s.users.find((x) => x.id === id);
        if (u) deleteUser(u);
      })
    );
  }

  return {
    destroy() {
      alive = false;
    },
  };
}

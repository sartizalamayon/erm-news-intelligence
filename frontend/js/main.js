// Entry point. Checks the session (GET /me), then mounts either the
// login screen or the authenticated app shell. Also wires the global
// 401 handler so any mid-session API 401 (expired/revoked session)
// drops the user back to login instead of showing a broken screen.

import { fetchMe } from "./auth.js";
import { setUnauthorizedHandler } from "./api.js";
import { mountLogin } from "./screens/login.js";
import { mountShell } from "./shell.js";
import { state, setState } from "./state.js";
import { getTheme, applyTheme } from "./theme.js";

const appRoot = document.getElementById("app");

applyTheme(getTheme());
setState({ theme: getTheme() });

let currentMount = null;

function showLogin() {
  if (currentMount && currentMount.destroy) currentMount.destroy();
  setState({ user: null, screen: "login" });
  currentMount = mountLogin(appRoot, { onAuthenticated: onAuthenticated });
}

function onAuthenticated(me) {
  setState({
    user: me,
    screen: "dash",
    pendingCount: undefined,
    approvedCount: undefined,
  });
  showShell();
}

function showShell() {
  if (currentMount && currentMount.destroy) currentMount.destroy();
  currentMount = mountShell(appRoot, { onSignOut: showLogin });
}

setUnauthorizedHandler(() => {
  if (state.screen !== "login") showLogin();
});

window.addEventListener("erm:signed-out", showLogin);

async function boot() {
  try {
    const me = await fetchMe();
    setState({ user: me, authChecked: true, screen: "dash" });
    showShell();
  } catch (err) {
    setState({ authChecked: true });
    showLogin();
  }
}

boot();

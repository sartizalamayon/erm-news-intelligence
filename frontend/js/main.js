// Entry point. Checks the session (GET /me), then mounts either the
// login screen or the authenticated app shell. Also wires the global
// 401 handler so any mid-session API 401 (expired/revoked session)
// drops the user back to login instead of showing a broken screen.

import { fetchMe } from "./auth.js";
import { setUnauthorizedHandler } from "./api.js";
import { mountLogin } from "./screens/login.js";
import { mountShell, applyRouteFromUrl, bindHistoryNavigation } from "./shell.js";
import { state, setState } from "./state.js";
import { getTheme, applyTheme } from "./theme.js";
import { resetSessionCaches } from "./util.js";

const appRoot = document.getElementById("app");

applyTheme(getTheme());
setState({ theme: getTheme() });

let currentMount = null;

function showLogin() {
  if (currentMount && currentMount.destroy) currentMount.destroy();
  // Whatever the previous session cached about "this user" and "this
  // deployment" is no longer trustworthy: the next sign-in may be a
  // different person with a different review queue and, if an admin
  // edited keywords in between, a different section list.
  resetSessionCaches();
  setState({ user: null, screen: "login" });
  currentMount = mountLogin(appRoot, { onAuthenticated: onAuthenticated });
}

// login()/setPassword() return { email, role, display_name } — enough
// to know auth succeeded, but not the full shell profile (initials,
// is_admin). Fetch GET /me once to get that, same as a normal boot.
async function onAuthenticated() {
  const me = await fetchMe();
  setState({
    user: me,
    pendingCount: undefined,
    approvedCount: undefined,
  });
  showShell();
  // AFTER the shell exists, so a deep link survives the login
  // round-trip: someone opening /review while signed out lands on
  // /review once authenticated, not on the Dashboard.
  applyRouteFromUrl();
}

// BACK-BUTTON BUG FIX: bindHistoryNavigation() lives HERE, next to the
// only call that puts the app shell on screen, and not in boot().
//
// It used to be the last statement of boot()'s try block, which meant it
// ran only when GET /me succeeded on page load — i.e. only for someone
// who arrived with a live session cookie. Everybody else took the other
// path: boot's /me 401s, showLogin() renders, the user signs in, and
// onAuthenticated() called showShell() and applyRouteFromUrl() but never
// this. So for every fresh sign-in, which is most sign-ins, `popstate`
// was never wired at all. Navigating Dashboard -> Review -> Export and
// pressing Back moved the address bar back to /review while the screen
// stayed on Export: the URL and the app disagreed, silently, for the
// rest of the session, and only a reload recovered. That is the exact
// failure route.js's header comment says the routing work exists to
// prevent.
//
// Binding here covers both paths by construction, because both reach
// the shell through this function. shell.js's `popstateBound` guard
// already makes the call idempotent, so the second caller is free.
function showShell() {
  if (currentMount && currentMount.destroy) currentMount.destroy();
  currentMount = mountShell(appRoot, { onSignOut: showLogin });
  bindHistoryNavigation();
}

setUnauthorizedHandler(() => {
  if (state.screen !== "login") showLogin();
});

window.addEventListener("erm:signed-out", showLogin);

async function boot() {
  try {
    const me = await fetchMe();
    setState({ user: me, authChecked: true });
    showShell();
    applyRouteFromUrl();
  } catch (err) {
    setState({ authChecked: true });
    showLogin();
  }
}

boot();

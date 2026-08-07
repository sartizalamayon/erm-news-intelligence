# ERM Desk frontend

Plain HTML + CSS + vanilla JS (ES modules). No npm, no build step, no
`node_modules` — the whole app is static files served as-is, per the
mission's disk-space constraint and ADR §1 (Render Static Site hosting).

## Running locally

Two processes: the API (FastAPI) and this static frontend.

**1. Start the API** (from the repo root, `/Users/ayo_on/Documents/backend`):

```
.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

**2. Serve the frontend** as static files — any static server works,
e.g.:

```
.venv/bin/python -m http.server 5500 --directory frontend
```

Then open `http://localhost:5500`. By default (see `js/config.js`) the
frontend talks to `http://localhost:8000` whenever it's running on
`localhost`/`127.0.0.1`/`file://` — no configuration needed for this
common case.

### Pointing at a different API

The API base URL is configuration, not a hardcoded constant sprinkled
through the code (`js/config.js` is the single place it's resolved):

- **One-off override:** open `http://localhost:5500/?api=https://your-api.example.com`
  — the value is saved to `localStorage` and the app reloads against it.
- **Persistent local override:** `localStorage.setItem('erm_api_base', 'http://localhost:9000')`
  in devtools, then reload.
- **Production default:** edit the `DEFAULT_PROD_API` constant at the
  top of `js/config.js` once the Render web service's real URL is
  known, before deploying the Render Static Site.

### CORS / cookies

The API's session cookie (`erm_session`) is `httpOnly`, and
`SameSite=None; Secure` in production/staging (frontend and API live on
different Render subdomains — see `app/services/auth.py`'s
`cookie_kwargs()`), or `SameSite=Lax` + non-secure for local
`http://localhost` dev. Every `fetch` call in `js/api.js` sends
`credentials: 'include'` so the cookie round-trips either way. For a
non-localhost deployment, set `CORS_ALLOW_ORIGINS` on the API to the
frontend's exact origin (see the repo root `.env.example`).

## Structure

```
frontend/
  index.html            shell page: mounts #app, loads css + js/main.js
  css/
    theme.css            CSS custom properties (dark/light tokens), verbatim from the design
    app.css               component styles (cards, buttons, tables, charts, review-queue layout, ...)
  js/
    config.js              API base URL resolution (see above)
    api.js                  fetch wrapper: credentials, JSON, error classification (network/401/403/404/...)
    util.js                  escapeHtml, tier() banding, real-max bar-width helpers, skeleton/empty/error renderers
    state.js                 tiny global store (auth/session, theme, current screen) + nav structure
    theme.js                  dark/light toggle, persisted to localStorage
    auth.js                   OTP request/verify/logout/me calls
    shell.js                  sidebar + topbar chrome, mounts the active screen
    router.js                  screen-key -> screen module lookup
    main.js                     entry point: session check -> login or shell
    screens/
      login.js                  OTP login (email -> code -> session)
      dashboard.js               short-term + long-term tabs
      review.js                  review queue (keyboard nav, bulk actions, dups/topics, notes)
      export.js                   export builder + presets
      brief.js                     Daily Risk Insight brief
      search.js                     archive search
      sources.js                     admin: sources CRUD + test
      keywords.js                     admin: keywords CRUD + rematch
      prompts.js                      admin: guidance/examples + candidates + provider routing/quota
      health.js                        admin: pipeline stages + source health + run history
      manual.js                         How it works (static explainer)
```

Every screen module exports `mount(root, ctx) -> { destroy() }`. There
is no framework and no virtual DOM: each module renders an HTML string
into its root element and re-renders on state changes (simple,
sufficient for this app's scale, and doesn't require reimplementing the
design tool's `<sc-for>`/`<sc-if>`/`support.js` runtime, which the
mission explicitly says not to copy).

## What's real vs. stubbed on the backend

`GET /me`, `POST /auth/otp/request`, `POST /auth/otp/verify`, `POST
/auth/logout` are real and this frontend is fully wired to them.

`sources.py`, `keywords.py`, `prompts.py`, and `health.py` (admin
screens) are real as of the ADMIN-API mission and this frontend is
wired to their actual response shapes (see the comment block at the top
of each corresponding `js/screens/*.js` file for the exact contract).

`dashboard.py` (the analytics `/dashboard/today` and
`/dashboard/trends` endpoints — distinct from ADMIN-API's
`/dashboard/storage/*`), `review.py`, `export.py`, `brief.py`, and
`search.py` are still stub routers with zero routes registered. Every
screen that depends on one of those calls the proposed endpoint shape
from `frontend-contract.md` §9, and renders a clearly-labelled "not
wired yet" banner plus the correct structural skeleton on a 404 —
never fabricated data. See this task's final report for the full list
of endpoints needed and their expected shapes.

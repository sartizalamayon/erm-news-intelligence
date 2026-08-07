"""Security response headers, applied to every response.

This API is a pure JSON backend (frontend-contract.md / ADR §1: the
frontend is a separate Render Static Site, a different origin). It never
renders HTML for a browser to display except FastAPI's own /docs and
/redoc pages, which app.main.create_app() disables outright in
production/staging (see that module) — so the strict, no-exceptions CSP
below never has to accommodate Swagger UI's CDN script/style needs in the
environment where it actually matters. In development/test, where docs
stay enabled, this middleware skips the CSP header on the docs paths
specifically rather than loosening it globally.

Headers sent, and why:

  * Strict-Transport-Security — tells the browser to only ever speak
    https to this origin for the given max-age, covering subdomains.
    Only sent when app_env is production/staging: Render terminates TLS
    at the edge and always forwards https in those environments, but
    local dev typically runs plain http, where HSTS would be actively
    wrong (it would tell the browser to refuse to fall back to http on
    the next request).
  * X-Content-Type-Options: nosniff — stops a browser from MIME-sniffing
    a response into something more dangerous than its declared
    Content-Type (defense against a stored-XSS-via-upload class of bug
    this app doesn't have today, cheap to send anyway).
  * X-Frame-Options: DENY + frame-ancestors 'none' (in the CSP) — this
    API must never be framed; belt-and-braces for older browsers that
    only honor X-Frame-Options and newer ones that prefer CSP.
  * Referrer-Policy: no-referrer — a JSON API has no legitimate use for
    the browser forwarding this origin's URL (which could include a
    session-bearing path segment or query string) to any other origin.
  * Content-Security-Policy — default-src 'none' is correct for an
    origin that returns application/json and nothing else: there is no
    script/style/image/font this origin ever intends a browser to
    execute or render as a page.
  * Permissions-Policy — a cheap extra: explicitly denies a handful of
    powerful browser features this origin has no use for, in case a
    response is ever loaded in a context that would otherwise inherit
    them.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# FastAPI's own docs UI needs CDN script/style access; only reachable
# when app.main.create_app() actually enables it (dev/test only — see
# that module). Excluded here by path rather than by environment so this
# module has no app_env branching duplicated from app.main's own gate.
_DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})

_STRICT_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
_PERMISSIONS_POLICY = "geolocation=(), camera=(), microphone=(), payment=(), usb=()"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, hsts_enabled: bool) -> None:
        super().__init__(app)
        self.hsts_enabled = hsts_enabled

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = _PERMISSIONS_POLICY

        if self.hsts_enabled:
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"

        if request.url.path not in _DOCS_PATHS:
            response.headers["Content-Security-Policy"] = _STRICT_CSP

        return response

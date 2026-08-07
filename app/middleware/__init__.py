"""Production security-hardening middleware (HARDEN mission).

Every module here is a plain Starlette/FastAPI middleware, applied
globally in `app.main.create_app()` via `app.add_middleware(...)` — never
enumerated per-route, so a router CORE-API adds concurrently is covered
automatically without this package needing to change (mission
constraint: "middleware must not assume a fixed route list").

  * security_headers.py — HSTS / X-Content-Type-Options / X-Frame-Options
    / Referrer-Policy / CSP on every response.
  * rate_limit.py        — in-process, per-client-IP general rate limit
    (distinct from app.services.auth's OTP-specific per-email/per-IP
    limiter, which this does not replace or weaken).
  * request_limits.py    — request body size cap + generic query-string
    / pagination bounds (protects the eventual GET /search free-text
    param and any `limit`/`page`/`offset` query param, wherever a router
    adds one).

See docs/security.md (news-intel-rebuild repo) for the full design
writeup and the settings that tune these.
"""

"""FastAPI application entrypoint.

Local run:      uvicorn app.main:app --reload
Render start:   uvicorn app.main:app --host 0.0.0.0 --port $PORT

`create_app()` is a factory (rather than a bare module-level `app`) so
tests can construct fresh instances with different settings if needed;
`app` below is the one real instance uvicorn imports.

Middleware stack (see docs/security.md, news-intel-rebuild repo, for the
full writeup) is added in a specific order because Starlette makes the
LAST middleware added the OUTERMOST layer (first to see the request,
last to see the response): CORS is added last on purpose so every
response — including a 429 from the rate limiter or a 413 from the body
size cap — still carries correct CORS headers back to the frontend
origin, rather than looking like an opaque CORS failure to the browser.
Actual add order below (first added = innermost): query/pagination
bounds, request body size cap, general rate limit, security headers,
CORS.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.error_handlers import register_exception_handlers
from app.api.routers import ALL_ROUTERS
from app.config import Settings, get_settings, validate_production_settings
from app.errors import ConfigurationError
from app.logging import configure_logging, get_logger
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_limits import BodySizeLimitMiddleware, QueryBoundsMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

logger = get_logger(__name__)

_PRODUCTION_LIKE = ("production", "staging")


def _validate_cors_settings(settings: Settings) -> None:
    """Fails fast rather than starting the process in a broken or
    dangerous CORS state.

    A literal "*" origin combined with allow_credentials=True (required
    here — the session cookie is cross-subdomain per ADR §1, SameSite=
    None; Secure) is not just bad practice, it is invalid per the
    Fetch/CORS spec: browsers refuse to honor a wildcard
    Access-Control-Allow-Origin on a credentialed request at all, and a
    server that ever attempted it would effectively be advertising "any
    origin, with cookies" to anything that ignored the browser and
    called the API directly. Reject it outright rather than silently
    doing something the browser would have blocked anyway.

    An empty allow-list in production/staging is a working-as-designed
    "CORS blocks everything" state (see the conditional below this
    function's caller), not a crash — but it almost certainly means the
    frontend origin was never configured, so it fails loudly here rather
    than shipping a backend the deployed frontend silently cannot reach.
    """
    origins = settings.cors_allow_origins_list
    if "*" in origins:
        raise ConfigurationError(
            "CORS_ALLOW_ORIGINS must not contain '*': this API requires "
            "allow_credentials=True (cross-subdomain session cookie, ADR "
            "§1), and a wildcard origin with credentials is invalid per "
            "the CORS spec as well as a real vulnerability. List the "
            "exact frontend origin(s) instead."
        )
    if settings.app_env in _PRODUCTION_LIKE and not origins:
        raise ConfigurationError(
            "CORS_ALLOW_ORIGINS is empty in a production-like environment "
            "(APP_ENV=" + settings.app_env + "). The deployed frontend "
            "origin will not be able to reach this API at all. Set "
            "CORS_ALLOW_ORIGINS explicitly."
        )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    validate_production_settings(settings)
    _validate_cors_settings(settings)

    production_like = settings.app_env in _PRODUCTION_LIKE
    # FastAPI's own interactive docs load their JS/CSS from a CDN, which
    # the strict CSP this app sends (app.middleware.security_headers)
    # does not allow, and which is not something an unauthenticated
    # internet-facing docs page needs to be reachable for anyway.
    # Disabled outright in production/staging; left on in dev/test where
    # they're genuinely useful and the CSP exemption in
    # SecurityHeadersMiddleware already accounts for them.
    docs_kwargs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if production_like else {}

    app = FastAPI(
        title="ERM News Intelligence API",
        version="0.1.0",
        description="Rebuilt news-intelligence platform API. See "
        "README-rebuild.md at the repo root for the project map.",
        # Explicit, not just relying on the default: FastAPI already
        # defaults to debug=False, but this is security-critical enough
        # (an unhandled exception under debug=True renders a full
        # traceback, including source snippets and local variables, as
        # the HTTP response body) to state rather than inherit silently.
        debug=False,
        **docs_kwargs,
    )

    # ── Middleware (see module docstring for why this exact order) ────
    app.add_middleware(
        QueryBoundsMiddleware,
        max_param_length=settings.max_query_param_length,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(
        RateLimitMiddleware,
        requests=settings.rate_limit_requests_per_window,
        window_seconds=settings.rate_limit_window_seconds,
        trusted_proxy_hops=settings.trusted_proxy_hops,
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts_enabled=production_like)

    if settings.cors_allow_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    for router in ALL_ROUTERS:
        app.include_router(router)

    @app.get("/healthz", tags=["infra"])
    def healthz() -> dict[str, str]:
        """Liveness probe. Deliberately has no dependencies (no DB call)
        so it reflects "is the process up," not "is the database up" —
        keep those separate if a readiness probe is added later.
        """
        return {"status": "ok"}

    logger.info("app_created", extra={"app_env": settings.app_env})
    return app


app = create_app()

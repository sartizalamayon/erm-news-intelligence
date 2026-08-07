"""Wires the app.errors taxonomy to actual HTTP responses.

Kept separate from app.errors itself so the exception hierarchy stays
importable from app.batch (no web dependency) while this module is
FastAPI-specific. Registered once, in app.main.create_app().

This is the concrete mechanism behind GATE 2's "zero unhandled
exceptions across the RED-TEAM suite": every AppError subclass gets a
deliberate status code here, and the final catch-all handler means a
genuinely unanticipated bug still returns a clean 500 with a logged
stack trace instead of an unhandled-exception stack trace leaking to
the client.

HARDEN mission additions (docs/security.md, news-intel-rebuild repo):
`RequestValidationError` (malformed JSON / a request that fails
pydantic's own schema check, e.g. a wrong type or a missing required
field — framework-level, distinct from app.errors.ValidationError which
is application code choosing to reject something) and a final
`Exception` catch-all, so that:

  * every error response this API ever returns — app-modeled or not —
    uses the same JSON envelope ({"error": ..., "detail": ...}), and
  * no response body, in any environment, ever contains a stack trace,
    a raw exception message from a lower layer (SQL, a driver error, a
    file path), or pydantic's internal per-field error structure. Full
    detail is always logged server-side (exc_info=...) — visible in
    Render/GitHub Actions logs, an operator's problem to read — never
    shipped back to the caller. This holds identically regardless of
    APP_ENV: FastAPI's own `debug` flag (explicitly `False` in
    app.main.create_app(), never toggled per-environment) is what would
    otherwise leak a traceback into a 500 body, and it never does here.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.errors import (
    AppError,
    ConfigurationError,
    ProviderError,
    ValidationError,
)
from app.logging import get_logger

logger = get_logger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # exc.errors() includes the raw submitted value per failing
        # field (pydantic's `input` key) -- a malformed OTP-code or
        # email submission could put something worth not casually
        # logging in there, so only (field location, error type) is
        # logged, never the submitted value, and neither ever reaches
        # the response body.
        fields = [{"loc": e.get("loc"), "type": e.get("type")} for e in exc.errors()]
        logger.warning("request_validation_error", extra={"path": request.url.path, "fields": fields})
        return JSONResponse(
            status_code=422,
            content={"error": "validation_error", "detail": "The request body failed validation."},
        )

    @app.exception_handler(ValidationError)
    async def _validation_error(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": "validation_error", "detail": str(exc)})

    @app.exception_handler(ProviderError)
    async def _provider_error(request: Request, exc: ProviderError) -> JSONResponse:
        # Retryable (rate-limited) -> 503 so a client/caller knows to
        # back off and retry; permanent -> 502 (upstream is the problem,
        # retrying won't help).
        status_code = 503 if exc.retryable else 502
        return JSONResponse(
            status_code=status_code,
            content={
                "error": "provider_error",
                "provider": exc.provider,
                "retryable": exc.retryable,
                "detail": str(exc),
            },
        )

    @app.exception_handler(ConfigurationError)
    async def _configuration_error(request: Request, exc: ConfigurationError) -> JSONResponse:
        # Configuration errors are meant to fail the process at startup
        # (see app.config.validate_production_settings) and never reach
        # a request handler. If one does anyway, log it loudly and
        # return 500 rather than let it crash the worker mid-request.
        logger.error("configuration_error_at_request_time", extra={"detail": str(exc)})
        return JSONResponse(status_code=500, content={"error": "configuration_error"})

    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError) -> JSONResponse:
        # Catch-all for taxonomy members without a more specific handler
        # above (e.g. a bare SourceError somehow reaching the API, which
        # shouldn't happen — batch isolates those — but this keeps the
        # response clean if it ever does).
        logger.error("unhandled_app_error", exc_info=exc, extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=500, content={"error": "internal_error"})

    @app.exception_handler(Exception)
    async def _unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        # Last resort: anything not modeled in app.errors at all (a raw
        # psycopg error, a KeyError from a real bug, ...). Without this,
        # Starlette's own ServerErrorMiddleware still returns a safe
        # generic response when FastAPI's debug=False (set explicitly in
        # app.main.create_app()) — no traceback leaks either way — but
        # its response is plain text, not this app's JSON envelope, and
        # this handler is what makes that "no traceback in production"
        # guarantee something a test can assert directly rather than
        # inherit implicitly from a framework default. Full exception
        # (type, message, traceback) goes to the logger only.
        logger.error("unhandled_exception", exc_info=exc, extra={"error_type": type(exc).__name__})
        return JSONResponse(status_code=500, content={"error": "internal_error"})

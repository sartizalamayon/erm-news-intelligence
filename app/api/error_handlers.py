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
"""

from __future__ import annotations

from fastapi import FastAPI, Request
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

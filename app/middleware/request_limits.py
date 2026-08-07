"""Request-shape validation that has nowhere else to live given this
mission's owned-files boundary (app/api/routers/** and app/api/schemas/**
belong to CORE-API/AUTH-OTP/ADMIN-API, not HARDEN) — so it is enforced
generically, at the middleware layer, in front of every route rather than
inside any one of them. That also means it automatically covers routes
CORE-API adds concurrently, including GET /search once it exists (today
it is a route-less stub — see app/api/routers/search.py — so there is no
`q=` parameter to bound yet; the query-length cap below already applies
the moment one is added, with no further change needed here).

Two independent guards:

  * BodySizeLimitMiddleware — rejects a request body over
    Settings.max_request_body_bytes, checked twice: once cheaply from a
    present Content-Length header (before anything reads the body), and
    once by counting bytes as the body actually streams in (covers a
    chunked-transfer-encoding request with no Content-Length, which the
    header check alone cannot catch). Raw ASGI middleware, not
    BaseHTTPMiddleware, because it needs to wrap `receive` itself.
  * QueryBoundsMiddleware — every query-string parameter value is capped
    at Settings.max_query_param_length (bounds free-text input like a
    future /search `q=` before it ever reaches a GIN index), and the
    common pagination parameter names (limit/page_size/per_page/page/
    offset) are additionally required to be a plausible, bounded integer
    -- this is a real fix, not just documentation: app/api/routers/health.py's
    `GET /health/runs?limit=5` has no upper bound in its own signature
    today (`limit: int = 5`), so `?limit=999999999` reaches the DB layer
    unmodified without this middleware in front of it.
"""

from __future__ import annotations

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

_PAGINATION_INT_PARAMS = frozenset({"limit", "page_size", "per_page", "page", "offset"})
# Generous enough that no legitimate current or near-future page size
# needs to exceed it (the largest today, keywords/prompts lists, is in
# the hundreds); an admin who genuinely needs more should paginate.
_MAX_PAGINATION_VALUE = 10_000


def _error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"error": "validation_error", "detail": detail}, status_code=status_code)


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = None
            if declared is not None and declared > self.max_bytes:
                response = _error(413, f"Request body exceeds the {self.max_bytes}-byte limit.")
                await response(scope, receive, send)
                return

        max_bytes = self.max_bytes
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body") or b"")
                if seen > max_bytes:
                    # Raised from inside the ASGI receive callable, which
                    # executes deep inside the app's own call stack (the
                    # route/body-parsing code calls `await receive()`) —
                    # so this propagates through Starlette's normal
                    # exception handling, not around it. FastAPI's
                    # built-in StarletteHTTPException handler turns this
                    # into a clean 413 JSON body; no different in kind
                    # from the 401/403/404s app/api/deps.py already
                    # raises the same way.
                    from fastapi import HTTPException

                    raise HTTPException(status_code=413, detail=f"Request body exceeds the {max_bytes}-byte limit.")
            return message

        await self.app(scope, limited_receive, send)


class QueryBoundsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, max_param_length: int, max_pagination_value: int = _MAX_PAGINATION_VALUE) -> None:
        super().__init__(app)
        self.max_param_length = max_param_length
        self.max_pagination_value = max_pagination_value

    async def dispatch(self, request: Request, call_next) -> Response:
        for key, value in request.query_params.multi_items():
            if len(value) > self.max_param_length:
                return _error(
                    400,
                    f"Query parameter '{key}' exceeds the {self.max_param_length}-character limit.",
                )
            if key.lower() in _PAGINATION_INT_PARAMS:
                try:
                    parsed = int(value)
                except ValueError:
                    return _error(400, f"Query parameter '{key}' must be an integer.")
                if key.lower() == "offset":
                    valid = 0 <= parsed <= self.max_pagination_value
                else:
                    valid = 1 <= parsed <= self.max_pagination_value
                if not valid:
                    return _error(
                        400,
                        f"Query parameter '{key}' must be between "
                        f"{'0' if key.lower() == 'offset' else '1'} and {self.max_pagination_value}.",
                    )

        return await call_next(request)

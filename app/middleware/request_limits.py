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
  * NulByteMiddleware — rejects a NUL (0x00) anywhere in the query
    string or a JSON body.

    Postgres cannot store NUL in a text field at all: psycopg raises
    `DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`.
    Nothing caught it, so a single stray byte turned EVERY free-text
    endpoint into an opaque 500. Measured: GET /search?q=, GET
    /articles?q=, POST /keywords, POST /sources and PUT
    /prompts/guidance all returned `{"error":"internal_error"}`.

    Rejected at the boundary rather than stripped, and generically
    rather than per-route, for the same reason as the two guards above:
    a route added next month inherits it. NUL is never meaningful in a
    headline, a keyword or a prompt, so a request containing one is
    malformed and should say so with a 400 instead of looking like a
    server fault.
"""

from __future__ import annotations

import json

from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

_PAGINATION_INT_PARAMS = frozenset({"limit", "page_size", "per_page", "page", "offset"})
# Generous enough that no legitimate current or near-future PAGE size
# needs to exceed it (the largest today, keywords/prompts lists, is in
# the hundreds); an admin who genuinely needs more per page should
# paginate instead.
_MAX_PAGINATION_VALUE = 10_000
# `offset` is not a page size, it is how deep into the corpus a page
# starts, and it legitimately grows with total row count rather than
# page size — sharing `_MAX_PAGINATION_VALUE` with `limit`/`page_size`
# was the bug: the Review queue's "Load more" (app/api/routers/review.py)
# sends `offset=<articles already loaded>`, and a real corpus comfortably
# exceeds 10,000 pending rows (18,453 measured live 2026-08-18), so an
# admin simply scrolling deep into a normal day's backlog hit a 400 that
# had nothing to do with anything actually being wrong. Bounded well
# above any realistic corpus size rather than left unlimited, so a
# still-pathological `offset` value is rejected instead of reaching the
# DB as a genuinely wasteful `OFFSET 999999999` scan.
_MAX_OFFSET_VALUE = 1_000_000


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
                is_offset = key.lower() == "offset"
                ceiling = _MAX_OFFSET_VALUE if is_offset else self.max_pagination_value
                valid = (0 <= parsed <= ceiling) if is_offset else (1 <= parsed <= ceiling)
                if not valid:
                    return _error(
                        400,
                        f"Query parameter '{key}' must be between {'0' if is_offset else '1'} and {ceiling}.",
                    )

        return await call_next(request)


# NUL is the one byte Postgres refuses outright in a text column, and it
# cannot carry meaning in any field this API accepts.
_NUL = "\x00"


def _contains_nul(value: object) -> bool:
    """True if a real U+0000 character appears anywhere in a decoded
    JSON value — recurses through dicts/lists, checks every string leaf.
    """
    if isinstance(value, str):
        return _NUL in value
    if isinstance(value, dict):
        return any(_contains_nul(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_nul(v) for v in value)
    return False


class NulByteMiddleware(BaseHTTPMiddleware):
    """400 on a NUL byte in the query string or a JSON body.

    Checks the RAW query string rather than parsed params: a percent
    encoded `%00` is decoded by the time `request.query_params` sees it,
    but so is an already-literal NUL, and reading the raw string catches
    both without depending on which form arrived.

    The body check is deliberately limited to JSON. Uploads
    (`POST /keywords/import` takes raw .xlsx bytes) are binary by
    nature and legitimately contain NULs, and the body has to stay
    readable by the route afterwards, so this only inspects a body it
    knows is text.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        raw_query = request.scope.get("query_string", b"")
        if b"%00" in raw_query.lower() or b"\x00" in raw_query:
            return _error(400, "Query string contains a NUL (0x00) byte, which is not valid text.")

        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            body = await request.body()
            # A raw byte scan alone MISSES an escaped NUL: a JSON encoder
            # never puts a raw 0x00 in the payload for an actual U+0000
            # character, it writes the six-character escape \u0000.
            # But that same substring also shows up, byte-for-byte
            # identical, in the wire form of a value whose real content
            # IS the literal six characters \u0000 (JSON-encoding
            # doubles that value's own backslash) -- a request with no
            # NUL anywhere in it. A substring search on the ENCODED bytes
            # cannot tell those two cases apart; only decoding can, so
            # the escape form below is a cheap pre-filter that decides
            # whether it's worth parsing, not the rejection test itself.
            if b"\x00" in body:
                return _error(400, "Request body contains a NUL (0x00) byte, which is not valid text.")
            if b"\\u0000" in body.lower():
                try:
                    decoded = json.loads(body)
                except ValueError:
                    decoded = None  # malformed JSON is the route's problem, not this guard's
                if decoded is not None and _contains_nul(decoded):
                    return _error(400, "Request body contains a NUL (0x00) byte, which is not valid text.")
            # `request.body()` caches on the Request and reads `receive`
            # to EOF. Starlette's BaseHTTPMiddleware wraps the downstream
            # app in a `_CachedRequest` that already replays that cached
            # body once `.body()` has been called (verified against the
            # installed Starlette version) -- nothing further is needed
            # to make the route see it. This used to reassign
            # `request._receive` believing that was what rewound the
            # stream; it demonstrably was not (this file's own tests
            # pass identically without it).

        return await call_next(request)

"""Shared HTTP layer — the ONE retry/backoff/session helper every source
uses, no exceptions (docs/speed-techniques.md §4 point 2, §3#3/#4).

Three things live here, and nothing else should reimplement any of them:

1. `make_session()` — one `requests.Session` per source, with the
   connection pool explicitly sized to that source's own worker count
   (otherwise concurrent fetches serialize on urllib3's default pool size
   of 10 — the exact bug speed-techniques.md §2 documents Repo B fixing).
2. `get()` — jittered exponential backoff (`1.5**attempt +
   random.uniform(0, 0.75)`, speed-techniques.md §4 point 2) around
   `session.request()`. Every source module calls THIS, never
   `requests.get()`/`session.get()` directly, mirroring the "one shared
   `_get()`, used uniformly" contract that Repo A never had
   (legacy-behavior.md's `get()` was skipped by 15+ call sites).
3. `GLOBAL_SEMAPHORE` — a process-wide cap on total in-flight HTTP
   requests (speed-techniques.md §4 point 3): free-tier infra has limited
   CPU/RAM/file descriptors, and Repo B (provisioned containers) doesn't
   need this. Every `get()` call acquires it, so ANY source's concurrency
   — including a source's own internal per-item thread pool (id_enum,
   daily_star_nodes) — is automatically bounded by the same global cap,
   without each source needing to know about it.

Deliberately excludes: per-source parsing logic (belongs in
`app/sources/_*.py` method-family helpers or a concrete source file),
and the process-wide semaphore's exact size is a tuned constant, not a
setting — free-tier infra doesn't get bigger by changing an env var.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from app.errors import SourceFetchError
from app.logging import get_logger

logger = get_logger(__name__)

# ── Shared HTTP identity ─────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,bn;q=0.8",
    "Connection": "keep-alive",
}

# SPLIT connect/read timeout, not one flat number.
#
# A single 10.0s budget conflates two very different failures and got
# both wrong. Measured live 2026-08-14 against the two sources the run
# panel was reporting as dead:
#
#     somoyjournal.com        HTTP 200 after 12.51s   <- alive, just slow
#     tob.news wp-json/...    HTTP 200 after  2.62s   <- fast alone,
#                                                        timed out under
#                                                        28-way concurrency
#
# Neither was unreachable. Both were being killed by an impatient read
# budget and then reported to the operator as "failed after retries (no
# response)" / "unreachable", which is actively misleading — it sends
# you looking at the wrong thing (the site) instead of the right one
# (our timeout).
#
# `requests` accepts `(connect, read)`. Separating them means a
# genuinely dead host still fails FAST (nothing listening -> connect
# fails in 5s, so a dead source cannot stall a 45-source run), while a
# host that is merely slow to generate a page gets the room it needs.
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
DEFAULT_TIMEOUT: tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT)
DEFAULT_MAX_RETRIES = 3
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# Process-wide concurrency ceiling (speed-techniques.md §4 point 3).
# Sized for free-tier infra (limited CPU/RAM/file descriptors) — Repo B
# runs on provisioned containers and doesn't need this. Every get() call
# acquires it, regardless of which source or which internal thread pool
# is making the call, so the true peak concurrent-connections number
# across the whole run is bounded here, in exactly one place.
GLOBAL_MAX_IN_FLIGHT = 28
GLOBAL_SEMAPHORE = threading.Semaphore(GLOBAL_MAX_IN_FLIGHT)

# Why the last transport exception is stashed rather than returned:
# `get()` returns `Response | None` and dozens of call sites depend on
# that shape, but `get_or_raise()` needs the CAUSE to write a message an
# operator can act on. Thread-local because sources run concurrently in
# a ThreadPoolExecutor — a module-level attribute would let one source's
# failure describe another's.
_last_error = threading.local()


def make_session(pool_size: int = 8) -> requests.Session:
    """One `requests.Session` per source, connection pool explicitly
    sized to `pool_size` (should be >= that source's own worker count —
    speed-techniques.md §2's "Repo B _bump_pool()" precedent). Reused for
    every call the source makes (feed discovery AND article bodies) so
    repeated calls to the same host reuse one TCP+TLS handshake instead
    of paying for a new one per request.
    """
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _backoff_seconds(attempt: int) -> float:
    """Jittered exponential backoff (speed-techniques.md §4 point 2):
    `1.5**attempt + random.uniform(0, 0.75)`. The jitter is not
    decorative — Repo B's own commit history (speed-techniques.md §2)
    shows un-jittered fixed-delay retries causing a retry-storm collapse
    (many concurrent workers retrying the same transient blip in
    lockstep, re-colliding every round).
    """
    return (1.5**attempt) + random.uniform(0, 0.75)


def _repair_encoding(response: requests.Response) -> None:
    """Guard against `requests`' ISO-8859-1 fallback mangling Bengali.

    Per RFC 2616, `requests` assumes ISO-8859-1 for a `text/*` response
    whose `Content-Type` carries no `charset`. Every byte then decodes
    to *something* — no exception, no warning — so a UTF-8 Bengali
    headline silently becomes mojibake, and `.text` is what almost every
    method in this package reads (`_id_enum`, `_body`, `_daily_star`,
    `_rss`, `_sitemap`, ...).

    Measured 2026-08-14: 0 of 53,646 stored titles show mojibake and
    29,723 hold correct Bengali, so no source we currently poll omits
    its charset. This is therefore a guard against a change we do not
    control — a CDN or CMS upgrade dropping the charset would otherwise
    corrupt a source's entire history before anyone noticed, and the
    corruption is unrecoverable once stored.

    Only overrides the ISO-8859-1 fallback and the no-encoding case; an
    explicitly declared charset is always respected.

    Reads everything through `getattr`: this sits on the return path of
    the one function every source's every HTTP call funnels through, so
    it must tolerate any response-like object (the test doubles across
    tests/ are plain dataclasses with only the fields their own case
    needs) rather than being able to raise and take a whole collection
    run down over a decoding nicety.
    """
    declared = (getattr(response, "encoding", None) or "").lower()
    if declared and declared != "iso-8859-1":
        return
    if not hasattr(response, "encoding"):
        return
    # `apparent_encoding` sniffs the actual bytes (charset_normalizer).
    # UTF-8 is the fallback-of-the-fallback: it is what every source in
    # this registry actually serves, and it fails loudly on genuinely
    # non-UTF-8 bytes rather than silently producing garbage.
    try:
        response.encoding = getattr(response, "apparent_encoding", None) or "utf-8"
    except (AttributeError, TypeError):  # frozen/read-only double
        return


def get(
    session: requests.Session,
    url: str,
    *,
    source: str,
    method: str = "GET",
    timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    headers: dict[str, str] | None = None,
    data: Any = None,
    params: dict[str, Any] | None = None,
    allow_redirects: bool = True,
) -> requests.Response | None:
    """The one retry/backoff call site every source funnels through.

    Returns the `requests.Response` for any response the server actually
    sent back (including non-200 — callers decide what a given status
    code means for their method: WP REST's 400 is "past the last page",
    not a failure, for instance). Returns `None` only when every attempt
    was exhausted without ANY response (pure network failure with
    `raise_on_exhaustion=False` semantics is NOT offered — see
    `get_or_raise` below for the source-boundary version); this bare
    `get()` is for call sites that want to treat "couldn't reach the
    host after retries" as an empty result rather than a hard failure
    (e.g. a fallback probe).
    """
    last_exc: Exception | None = None
    _last_error.exc = None
    for attempt in range(max_retries + 1):
        try:
            with GLOBAL_SEMAPHORE:
                response = session.request(
                    method,
                    url,
                    timeout=timeout,
                    headers=headers,
                    data=data,
                    params=params,
                    allow_redirects=allow_redirects,
                )
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if attempt < max_retries:
                time.sleep(_backoff_seconds(attempt))
                continue
            _last_error.exc = exc
            logger.warning(
                "http_get_exhausted",
                extra={
                    "source": source,
                    "url": url,
                    "attempts": attempt + 1,
                    "error": type(exc).__name__,
                    "diagnosis": _describe_failure(exc),
                },
            )
            return None
        if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
            time.sleep(_backoff_seconds(attempt))
            continue
        _repair_encoding(response)
        return response
    if last_exc is not None:
        _last_error.exc = last_exc
        logger.warning("http_get_failed", extra={"source": source, "url": url, "error": type(last_exc).__name__})
    return None


def _describe_failure(exc: Exception | None) -> str:
    """Turn the last transport exception into something an operator can
    ACT on.

    The old message for every network failure was "failed after retries
    (no response)", which reads as "the site is down" and sent us
    investigating two perfectly healthy sources. A read timeout, a
    refused connection, and a DNS failure need three different responses
    from a human, so they get three different messages.
    """
    if exc is None:
        return "no response"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return f"could not establish a connection within {CONNECT_TIMEOUT:.0f}s (host not accepting connections)"
    if isinstance(exc, requests.exceptions.ReadTimeout):
        return f"connected, but sent no response within {READ_TIMEOUT:.0f}s (host is alive but too slow)"
    if isinstance(exc, requests.exceptions.SSLError):
        return f"TLS handshake failed ({type(exc).__name__})"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "connection refused or DNS lookup failed"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return "redirect loop"
    return f"{type(exc).__name__}"


def get_or_raise(
    session: requests.Session,
    url: str,
    *,
    source: str,
    **kwargs: Any,
) -> requests.Response:
    """Like `get()`, but raises `SourceFetchError` instead of returning
    `None` when every retry is exhausted. Use this at a source's PRIMARY
    entry point (e.g. the one homepage/sitemap/API fetch a whole
    strategy depends on) so a dead host surfaces as an isolable,
    per-source failure (app/errors.py's taxonomy) rather than silently
    yielding zero items — which speed-techniques.md §5's "loud-fail
    guard" explicitly warns collapses "blocked/broken" into
    "success, 0 found".
    """
    response = get(session, url, source=source, **kwargs)
    if response is None:
        raise SourceFetchError(
            f"GET {url} failed after retries — {_describe_failure(getattr(_last_error, 'exc', None))}",
            source=source,
        )
    return response

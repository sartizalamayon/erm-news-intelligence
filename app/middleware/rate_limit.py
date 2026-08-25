"""General, whole-API rate limiting — distinct from and in addition to
app.services.auth's OTP-specific per-email (3/15min) and per-IP
(10/15min) limiter. That limiter protects the Resend daily send quota
specifically; this one protects the whole process (every route, every
method) from a single client hammering it, which the OTP limiter was
never meant to cover (it only ever counts otp_codes rows).

In-process, sliding-window-by-truncation counter keyed by client IP, held
in a plain dict for the lifetime of the worker process. This is a
deliberate, documented limitation: GATE 0 has no Redis (single Render
instance, no declared dependency for a shared store), so a limit here
only holds per process. On a single instance (the current deploy shape,
ADR §1) that is the whole story. If this API is ever scaled to more than
one Render instance, this stops being a global limit — each instance
enforces its own — and a shared store (Redis, or Postgres-backed
counters) would be required to keep the guarantee; flagged here rather
than silently assumed away.

Safe under FastAPI's normal (single-process, asyncio event loop)
deployment: the check-then-increment below contains no `await`, so it is
atomic with respect to every other coroutine on the same event loop
without needing an explicit lock. It is NOT safe across multiple worker
*processes* (e.g. `uvicorn --workers N`) — same limitation as above,
each process would keep its own counters.

IP resolution deliberately mirrors app.api.routers.auth._client_ip's RT-5
remediation (docs/red-team-findings.md) — same "rightmost trusted hop"
model, driven by the same app.config.Settings.trusted_proxy_hops field —
rather than importing that (private, AUTH-OTP-owned) function directly.
Keeping this a short, independent read of the same setting avoids a
cross-package dependency on another agent's private helper while still
extending the exact same, already-reviewed security pattern; if
trusted_proxy_hops ever changes meaning, both call sites read the same
field and move together.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# Paths exempt from the general limiter — every health probe this
# deployment exposes, not just the one that happened to exist when this
# middleware was written.
#
#   * `/healthz`      — the zero-dependency liveness probe declared in
#     app.main (its own docstring: "deliberately has no dependencies").
#   * `/health/ready` — the readiness probe in app.api.routers.health,
#     and the value render.yaml actually gives `healthCheckPath`. It was
#     NOT exempt, and that asymmetry was invisible because the exemption
#     set named the route nobody configured and omitted the route
#     everybody uses.
#
# What the omission actually risks: Render polls `healthCheckPath` to
# decide both when a deploy goes live and whether to restart a wedged
# instance. 120 requests/minute is far above any health-checker's own
# poll rate, so the probe cannot exhaust the bucket by itself — the
# failure needs a second ingredient, and this deployment has one. The
# limiter keys on the RESOLVED client IP, which behind Render's proxy
# with TRUSTED_PROXY_HOPS=1 is the rightmost X-Forwarded-For entry; if
# Render's checker ever arrives without that header (or shares a
# resolved address with live traffic), it lands in the same bucket as
# real users. A burst of ordinary traffic then answers the health check
# with a 429, Render reads a non-2xx as unhealthy, restarts the
# instance — and the restart drops the in-process counters, so the
# evidence of why is gone by the time anyone looks. A rate limiter must
# never be able to take the platform's own view of "is this service
# alive" hostage.
#
# tests/test_health_probe_rate_limit_exemption.py parses render.yaml's
# real `healthCheckPath` and asserts it is a member of this set, so the
# two cannot drift apart again the way they already did once: this file
# and the blueprint are edited by different people at different times,
# and nothing but that test connects them.
_EXEMPT_PATHS = frozenset({"/healthz", "/health/ready"})


def resolve_client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """Same algorithm, same reasoning, as
    app.api.routers.auth._client_ip -- see that function's docstring for
    the full RT-5 write-up. hops <= 0 (the fail-safe default) never
    consults X-Forwarded-For at all.
    """
    if trusted_proxy_hops > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
            if len(entries) >= trusted_proxy_hops:
                return entries[-trusted_proxy_hops]
    client = request.client
    return client.host if client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        *,
        requests: int,
        window_seconds: int,
        trusted_proxy_hops: int,
    ) -> None:
        super().__init__(app)
        self.requests = requests
        self.window_seconds = window_seconds
        self.trusted_proxy_hops = trusted_proxy_hops
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        ip = resolve_client_ip(request, self.trusted_proxy_hops)
        now = time.monotonic()
        cutoff = now - self.window_seconds

        bucket = self._hits[ip]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self.requests:
            retry_after = max(1, int(bucket[0] + self.window_seconds - now))
            return JSONResponse(
                {"error": "rate_limited", "detail": "Too many requests. Slow down and try again shortly."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )

        if not bucket:
            # Trimmed down to nothing above — drop the now-empty deque
            # rather than let one accumulate per distinct IP ever seen
            # for the life of the process (unbounded memory growth from
            # one-off/bot traffic). defaultdict re-creates it on the
            # next line, same object identity concern either way.
            del self._hits[ip]
        self._hits[ip].append(now)

        return await call_next(request)

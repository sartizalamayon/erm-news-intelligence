"""RED-TEAM: OTP rate-limit bypass (mission attack surface "Auth
adversarial cases ... rate-limit bypass via case/whitespace/plus-
addressing variants of the same email ... IP spoofing via forwarded
headers").

Two independent bypasses found against app.services.auth's rate
limiting (docs/red-team-findings.md RT-4 / RT-5):

RT-4 -- plus-addressing defeats the per-email limit
----------------------------------------------------------------------
app.services.auth.normalize_email() is `email.strip().lower()` only --
case and surrounding whitespace ARE folded (so `User@GP.com` and
` user@gp.com ` correctly collide), but a `+tag` in the local part is
NOT stripped. `_enforce_rate_limits()`'s per-email counter
(`WHERE email = %s`) matches on this same un-folded string, so
`person@grameenphone.com`, `person+1@grameenphone.com`,
`person+2@grameenphone.com`, ... are distinct rows to the rate limiter
even though most real mail systems (Gmail-style plus-addressing is
extremely common, and RFC 5233 "Sieve" subaddress extension is
widely deployed on corporate mail too) deliver all of them to the exact
same mailbox. `OTP_RATE_LIMIT_PER_EMAIL = 3` per 15 minutes is
trivially defeated by incrementing the tag on every request; each such
request also creates a brand-new `users` row on first successful
verify (auth_service._get_or_create_user matches on the exact email
string too), so this also lets one real mailbox accumulate unbounded
distinct accounts.

RT-5 -- spoofed X-Forwarded-For defeats the per-IP limit
----------------------------------------------------------------------
app.api.routers.auth._client_ip() takes `request.headers["x-forwarded-for"].split(",")[0]`
unconditionally -- the LEFTMOST entry. Its own comment claims "the first
hop is trustworthy" because Render "sets X-Forwarded-For itself", but
that reasoning only holds if Render's proxy REPLACES the header. Every
standard reverse-proxy behavior (Render/nginx/most L7 proxies included)
APPENDS the real client IP as its own hop rather than discarding
whatever the client already sent -- so a client that sends its own
`X-Forwarded-For: <anything>` gets `"<anything>, <real proxy-observed
ip>"` on the wire, and `.split(",")[0]` reads back exactly the
attacker-supplied value. `OTP_RATE_LIMIT_PER_IP = 10` per 15 minutes is
defeated by sending a different spoofed value on every request.
"""

from __future__ import annotations

import contextlib
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app.api.deps as deps_module
import app.api.routers.auth as auth_router_module
from app.db.testing import migrated_schema, pg_available
from app.main import create_app
from app.services import auth as auth_service

pg_gated = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ALLOWED_DOMAIN = "grameenphone.com"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch):
    from app.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-do-not-use-in-prod")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_send_ok(monkeypatch: pytest.MonkeyPatch) -> Mock:
    fake = Mock(return_value=None)
    monkeypatch.setattr(auth_service, "send_otp_email", fake)
    return fake


# ── RT-4: plus-addressing bypasses the per-email rate limit ─────────────


@pg_gated
@pytest.mark.xfail(
    strict=True,
    reason="RT-4 (docs/red-team-findings.md): app.services.auth.normalize_email() does not fold "
    "'+tag' local-part variants, so OTP_RATE_LIMIT_PER_EMAIL (3 per 15 min) is bypassed by "
    "requesting codes for person+1@, person+2@, person+3@... grameenphone.com -- all of which "
    "deliver to the same real mailbox under common plus-addressing mail routing. Owner: AUTH-OTP.",
)
def test_plus_addressing_bypasses_per_email_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        _fake_send_ok(monkeypatch)
        base = f"ratelimit.target@{ALLOWED_DOMAIN}"

        # OTP_RATE_LIMIT_PER_EMAIL requests against the bare address --
        # this much is expected to succeed (it's exactly at the limit,
        # not over it).
        for _ in range(auth_service.OTP_RATE_LIMIT_PER_EMAIL):
            auth_service.request_otp(conn, email=base, ip_address="203.0.113.10")

        # One request too many against the SAME address must now be
        # blocked -- this is the control, proving the limiter works at all.
        with pytest.raises(auth_service.OtpRateLimited):
            auth_service.request_otp(conn, email=base, ip_address="203.0.113.10")

        # Correct (secure) behavior: a rate limiter that properly folds
        # +tag variants of the same mailbox would eventually raise
        # OtpRateLimited again here, same as the control above. Caught
        # explicitly (not let to propagate) so this test's pass/fail
        # reflects the ASSERTION below, not "did some exception happen
        # to propagate" -- that keeps xfail's strict=True meaningful: it
        # flips to an unexpected pass (and fails the suite, on purpose)
        # only once request_otp() genuinely starts raising here.
        local, domain = base.split("@", 1)
        eventually_rate_limited = False
        try:
            for i in range(5):
                tagged = f"{local}+{i}@{domain}"
                auth_service.request_otp(conn, email=tagged, ip_address="203.0.113.10")
        except auth_service.OtpRateLimited:
            eventually_rate_limited = True

        assert eventually_rate_limited, (
            "plus-addressed variants of the same mailbox (person+0@, person+1@, ...) were never "
            "rate-limited -- OTP_RATE_LIMIT_PER_EMAIL was bypassed"
        )


# ── RT-5: spoofed X-Forwarded-For bypasses the per-IP rate limit ────────


@pg_gated
@pytest.mark.xfail(
    strict=True,
    reason="RT-5 (docs/red-team-findings.md): app.api.routers.auth._client_ip() trusts the "
    "leftmost X-Forwarded-For entry unconditionally, which is exactly the client-controlled "
    "position when a proxy appends (rather than replaces) the header. OTP_RATE_LIMIT_PER_IP "
    "(10 per 15 min) is bypassed by sending a fresh spoofed X-Forwarded-For value on every "
    "request. Owner: AUTH-OTP.",
)
def test_spoofed_x_forwarded_for_bypasses_per_ip_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        monkeypatch.setattr(deps_module, "get_connection", lambda: contextlib.nullcontext(conn))
        _fake_send_ok(monkeypatch)

        with TestClient(create_app()) as client:
            # Exhaust the per-IP limit for real, from what looks like one
            # real client IP (TestClient's default request.client.host),
            # by varying the EMAIL so the per-email limiter doesn't
            # interfere -- this isolates the IP dimension, matching
            # tests/test_auth_service.py::test_request_otp_rate_limited_per_ip's
            # own isolation approach.
            for i in range(auth_service.OTP_RATE_LIMIT_PER_IP):
                resp = client.post(
                    "/auth/otp/request",
                    json={"email": f"person{i}@{ALLOWED_DOMAIN}"},
                )
                assert resp.status_code == 202

            # One more from the same (real) IP must now be blocked --
            # control, proving the limiter is live end-to-end over HTTP.
            blocked = client.post(
                "/auth/otp/request",
                json={"email": f"one-too-many@{ALLOWED_DOMAIN}"},
            )
            assert blocked.status_code == 429

            # Correct (secure) behavior: if _client_ip() weren't fooled by
            # a client-supplied X-Forwarded-For, these requests would
            # still be attributed to the one real connecting IP and one
            # of them would eventually come back 429, same as the
            # control above.
            eventually_rate_limited = False
            for i in range(5):
                spoofed_ip = f"198.51.100.{i}"
                resp = client.post(
                    "/auth/otp/request",
                    json={"email": f"spoofed{i}@{ALLOWED_DOMAIN}"},
                    headers={"X-Forwarded-For": spoofed_ip},
                )
                if resp.status_code == 429:
                    eventually_rate_limited = True
                    break

        assert eventually_rate_limited, (
            "requests with a fresh spoofed X-Forwarded-For header on every call were never "
            "rate-limited -- OTP_RATE_LIMIT_PER_IP was bypassed"
        )

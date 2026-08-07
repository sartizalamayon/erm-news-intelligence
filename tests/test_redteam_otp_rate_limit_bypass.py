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

BOTH FIXED by AUTH-OTP (commit 00224e5), independently re-verified below
by attacking the fix itself, not just re-running the original
reproduction:

RT-4's fix -- `app.services.auth._rate_limit_email_key()` folds a
`+tag` local-part suffix (Python side) and
`_EMAIL_RATE_LIMIT_CANONICAL_SQL`, a fixed SQL expression applying the
identical fold to the stored `email` column (SQL side), so the rate
limit is enforced against the CANONICAL identity on both sides of the
comparison. Deliberately does NOT fold dots (Gmail-only quirk, not
SMTP-standard) -- re-verified this is the correct call for the real
`grameenphone.com` domain specifically, not just defensible in the
abstract: `dig +short MX grameenphone.com` resolves to
`grameenphone-com.mail.protection.outlook.com`, i.e. Microsoft
365/Exchange Online, which does NOT fold dots in mailbox local parts
the way Gmail does -- folding them would have wrongly merged distinct
mailboxes for this exact production domain. (DNS lookup, not a live
news site or an LLM call -- allowed under mission scope; not re-run
inside the test suite itself, which must not depend on live network --
the finding is recorded here as verified research, and the test below
pins the code's dot-preserving BEHAVIOR, which needs no network.)

RT-5's fix -- `app.config.Settings.trusted_proxy_hops` (default `0`,
fail-safe validated) plus `app.api.routers.auth._client_ip()`'s
rightmost-`hops`-th selection, immune to a client left-padding the
header with extra fake entries (a trusted hop always APPENDS, so the
attacker never controls a position measured from the right).
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
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
def test_plus_addressing_bypasses_per_email_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-4, CONFIRMED FIXED (AUTH-OTP, 00224e5) -- was xfail(strict=True)."""
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
def test_spoofed_x_forwarded_for_bypasses_per_ip_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-5, CONFIRMED FIXED (AUTH-OTP, 00224e5) -- was xfail(strict=True)."""
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


# ── Second-pass attacks on the fixes themselves (coordinator's specific ──
# ── angles), not just re-running the original reproduction ──────────────


@pg_gated
def test_sql_side_and_python_side_plus_tag_folding_agree_for_odd_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attacks RT-4's fix directly: `_rate_limit_email_key()` (Python,
    used to compute the incoming request's canonical key) and
    `_EMAIL_RATE_LIMIT_CANONICAL_SQL` (SQL, used to canonicalize every
    STORED row for comparison) are two independently-written expressions
    that must agree exactly, or the rate limit silently stops applying
    for whichever shape they disagree on -- a live bypass, per the
    coordinator's explicit flag. Tries the odd shapes most likely to
    expose a mismatch: mixed case + plus-tag together, surrounding
    whitespace + plus-tag together, a doubled '+', and a trailing bare
    '+' with no tag at all.
    """
    with migrated_schema() as conn:
        _fake_send_ok(monkeypatch)
        local = "oddshapes.target"
        domain = ALLOWED_DOMAIN

        # Exactly at the limit with the bare address.
        for _ in range(auth_service.OTP_RATE_LIMIT_PER_EMAIL):
            auth_service.request_otp(conn, email=f"{local}@{domain}", ip_address="203.0.113.20")

        odd_variants = [
            f"{local.title()}+1@{domain.upper()}",  # mixed case + tag
            f"  {local}+2@{domain}  ",  # whitespace + tag
            f"{local}++3@{domain}",  # doubled '+'
            f"{local}+@{domain}",  # trailing bare '+', empty tag
        ]
        blocked_count = 0
        for variant in odd_variants:
            try:
                auth_service.request_otp(conn, email=variant, ip_address="203.0.113.20")
            except auth_service.OtpRateLimited:
                blocked_count += 1

        assert blocked_count == len(odd_variants), (
            f"expected every odd-shaped plus-tag variant to be caught by the already-tripped "
            f"per-email limit (SQL-side and Python-side folding must agree) -- only "
            f"{blocked_count}/{len(odd_variants)} were blocked, meaning at least one shape "
            "bypasses the fix"
        )


@pg_gated
def test_dot_variants_are_deliberately_not_folded_and_stay_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins RT-4's fix's deliberate NON-fix: dots are not folded, so
    `person.dot@grameenphone.com` and `persondot@grameenphone.com` are
    treated as genuinely different addresses for rate-limiting, each
    getting their own OTP_RATE_LIMIT_PER_EMAIL budget. Verified correct
    for the real domain (module docstring: grameenphone.com's MX records
    resolve to Microsoft 365/Exchange Online, which does not fold dots
    the way Gmail does) -- this test only pins the code's behavior, no
    network access.
    """
    with migrated_schema() as conn:
        _fake_send_ok(monkeypatch)

        # Exhaust the limit for the dotted address...
        for _ in range(auth_service.OTP_RATE_LIMIT_PER_EMAIL):
            auth_service.request_otp(conn, email=f"person.dot@{ALLOWED_DOMAIN}", ip_address="203.0.113.30")
        with pytest.raises(auth_service.OtpRateLimited):
            auth_service.request_otp(conn, email=f"person.dot@{ALLOWED_DOMAIN}", ip_address="203.0.113.30")

        # ...the dot-free variant must be COMPLETELY unaffected -- a
        # regression here (accidentally folding dots) would silently
        # merge two potentially-real, distinct mailboxes.
        auth_service.request_otp(conn, email=f"persondot@{ALLOWED_DOMAIN}", ip_address="203.0.113.30")


def test_trusted_proxy_hops_defaults_to_zero_and_ignores_x_forwarded_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attacks RT-5's fix directly at the unit level (no DB, no HTTP):
    with TRUSTED_PROXY_HOPS unset (the real deployment default), any
    X-Forwarded-For value -- however elaborate -- must be completely
    ignored in favor of the direct socket peer.
    """
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    fake_request = SimpleNamespace(
        headers={"x-forwarded-for": "1.2.3.4, 5.6.7.8, 9.10.11.12"},
        client=SimpleNamespace(host="203.0.113.55"),
    )
    assert auth_router_module._trusted_proxy_hops() == 0
    assert auth_router_module._client_ip(fake_request) == "203.0.113.55"


@pytest.mark.parametrize("misconfigured_value", ["-1", "-99", "abc", "1.5", "", "  ", "0x1"])
def test_trusted_proxy_hops_fails_safe_on_misconfiguration(
    monkeypatch: pytest.MonkeyPatch, misconfigured_value: str
) -> None:
    """Attacks RT-5's fix's fail-safe claim directly: a negative,
    non-numeric, or otherwise malformed TRUSTED_PROXY_HOPS must resolve
    to 0 (trust nothing), never raise, and never be silently treated as
    a large/positive number of hops.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", misconfigured_value)
    assert auth_router_module._trusted_proxy_hops() == 0


def test_trusted_proxy_hops_selects_the_correct_rightmost_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attacks RT-5's fix's core selection logic: with hops=1 configured
    (a single trusted proxy, e.g. Render alone), the correct entry is
    the RIGHTMOST one -- the one a trusted hop appended -- regardless of
    how many attacker-controlled fake entries are prepended to the left.
    A client cannot shift which position is "rightmost" no matter how it
    pads the header, since a trusted hop always APPENDS rather than
    replacing.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    # No padding at all.
    req1 = SimpleNamespace(headers={"x-forwarded-for": "9.9.9.9"}, client=SimpleNamespace(host="unused"))
    assert auth_router_module._client_ip(req1) == "9.9.9.9"

    # Heavily left-padded with attacker-chosen fake entries -- the real
    # (rightmost, trusted-hop-appended) entry must still win.
    req2 = SimpleNamespace(
        headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3, 9.9.9.9"},
        client=SimpleNamespace(host="unused"),
    )
    assert auth_router_module._client_ip(req2) == "9.9.9.9"

    # hops=2 (an additional trusted CDN/WAF in front of Render): the
    # SECOND-from-right entry is the one the first trusted hop appended.
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    req3 = SimpleNamespace(
        headers={"x-forwarded-for": "1.1.1.1, 8.8.8.8, 9.9.9.9"},
        client=SimpleNamespace(host="unused"),
    )
    assert auth_router_module._client_ip(req3) == "8.8.8.8"

    # Fewer entries than configured hops -- must fail safe to the direct
    # peer rather than index into a too-short list or pick a wrong entry.
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "5")
    req4 = SimpleNamespace(
        headers={"x-forwarded-for": "1.1.1.1, 2.2.2.2"},
        client=SimpleNamespace(host="203.0.113.77"),
    )
    assert auth_router_module._client_ip(req4) == "203.0.113.77"

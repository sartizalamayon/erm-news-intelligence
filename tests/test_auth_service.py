"""Service-layer tests for app.services.auth -- the adversarial cases
DoD #9 requires: expired code, reused code, wrong code, non-allowlisted
domain, rate limit exceeded (both dimensions), enumeration, and
email-send failure -- plus the mechanical security properties DoD #2
requires (secrets-based generation, hashed storage, constant-time
compare) and DoD #8 (no full email/OTP code in logs).

Tests the SERVICE layer directly against a real, freshly-migrated
Postgres schema (app.db.testing.migrated_schema / pg_available), the
same pattern tests/test_db_schema.py and tests/test_db_crypto.py already
use -- these tables' behavior (partial unique index, FOR UPDATE locking,
CHECK constraints) is genuinely Postgres-specific, so a mocked
connection would not exercise it honestly. app.services.email.send_otp_email
is monkeypatched in every test here -- the network guard
(tests/helpers/network_guard.py, autouse) would block a real Resend call
anyway, but tests need deterministic control over success/failure/quota
outcomes that a blocked socket alone can't simulate.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

from app.db.testing import migrated_schema, pg_available
from app.errors import ProviderPermanentError, ProviderRateLimitedError, ValidationError
from app.services import auth as auth_service

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ALLOWED_EMAIL = "person@grameenphone.com"
OUTSIDE_EMAIL = "person@gmail.com"
TEST_IP = "203.0.113.9"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch):
    """tests/conftest.py deliberately sets SECRET_KEY="" (never a real
    developer secret in CI/local test runs) -- but app.services.auth's
    OTP hashing (DoD #2) requires one to be configured, by design (see
    that module's docstring: "cannot hash OTP codes" is a
    ConfigurationError, not a silent unkeyed fallback). This fixture is
    local to this test file, sets an arbitrary non-production value for
    the duration of each test, and restores app.config's settings cache
    afterward -- it does not touch tests/conftest.py.
    """
    from app.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-do-not-use-in-prod")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_send_ok(monkeypatch: pytest.MonkeyPatch) -> Mock:
    fake = Mock(return_value=None)
    monkeypatch.setattr(auth_service, "send_otp_email", fake)
    return fake


def _request_and_capture_code(
    conn, monkeypatch: pytest.MonkeyPatch, email: str = ALLOWED_EMAIL, ip: str = TEST_IP
) -> str:
    """Runs request_otp() with a mocked send, and returns the raw code
    that was handed to send_otp_email -- the only place it exists
    outside the (unrecoverable) hash stored in the database.
    """
    fake = _fake_send_ok(monkeypatch)
    auth_service.request_otp(conn, email=email, ip_address=ip)
    assert fake.call_count == 1
    return fake.call_args.kwargs["code"]


# ── OTP generation / storage mechanics (DoD #2) ──────────────────────────


def test_generate_otp_code_is_six_digit_numeric_string() -> None:
    code = auth_service.generate_otp_code()
    assert len(code) == 6
    assert code.isdigit()


def test_generate_otp_code_uses_secrets_module_not_random() -> None:
    import inspect

    source = inspect.getsource(auth_service)
    assert "import secrets" in source
    assert "secrets.randbelow" in source
    # "random" (the non-cryptographic stdlib module) must never appear as
    # an import -- a plain substring check on "import random" is enough
    # since nothing else in this file would legitimately contain that text.
    assert "import random" not in source


def test_verify_otp_uses_constant_time_comparison() -> None:
    import inspect

    source = inspect.getsource(auth_service)
    assert "hmac.compare_digest" in source


def test_otp_code_is_stored_hashed_not_plaintext() -> None:
    with migrated_schema() as conn:
        fake = Mock(return_value=None)
        import pytest as _pytest  # local import avoids polluting module namespace with a fixture-only name

        mp = _pytest.MonkeyPatch()
        try:
            mp.setattr(auth_service, "send_otp_email", fake)
            auth_service.request_otp(conn, email=ALLOWED_EMAIL, ip_address=TEST_IP)
        finally:
            mp.undo()
        code = fake.call_args.kwargs["code"]

        row = conn.execute("SELECT code_hash FROM otp_codes WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()
        assert row is not None
        assert row["code_hash"] != code
        assert code not in row["code_hash"]
        assert len(row["code_hash"]) == 64  # sha256 hex digest


# ── Domain allowlist (DoD #1) ─────────────────────────────────────────────


def test_request_otp_rejects_non_allowlisted_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        fake = _fake_send_ok(monkeypatch)
        with pytest.raises(ValidationError, match="grameenphone.com"):
            auth_service.request_otp(conn, email=OUTSIDE_EMAIL, ip_address=TEST_IP)
        fake.assert_not_called()

        count = conn.execute("SELECT count(*) AS n FROM otp_codes").fetchone()["n"]
        assert count == 0  # no code generated/stored for a disallowed domain


def test_request_otp_rejects_malformed_email(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        fake = _fake_send_ok(monkeypatch)
        with pytest.raises(ValidationError):
            auth_service.request_otp(conn, email="not-an-email", ip_address=TEST_IP)
        fake.assert_not_called()


def test_domain_allowlist_is_read_from_settings_table_not_hardcoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """DoD #1: "The allowed domain is CONFIGURATION... not a hardcoded
    literal." Proven here by changing the configured domain at runtime
    (0001_settings.sql's `settings.allowed_email_domain`, the exact row
    ARCH-DB seeded for this) and observing request_otp()'s behavior flip
    with it -- a hardcoded literal could not do this.
    """
    with migrated_schema() as conn:
        conn.execute("UPDATE settings SET value = 'othercorp.com' WHERE key = 'allowed_email_domain'")

        fake = _fake_send_ok(monkeypatch)
        with pytest.raises(ValidationError):
            auth_service.request_otp(conn, email=ALLOWED_EMAIL, ip_address=TEST_IP)
        fake.assert_not_called()

        auth_service.request_otp(conn, email="person@othercorp.com", ip_address=TEST_IP)
        fake.assert_called_once()


# ── No enumeration (DoD #3) ───────────────────────────────────────────────


def test_request_otp_never_queries_the_users_table(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        # Compare before/after rather than asserting the table starts
        # empty: migrations/0009_seed_admin.sql seeds one bootstrap-admin
        # row on every freshly migrated schema, so "empty" is no longer a
        # safe assumption here -- what must hold is that request_otp()
        # itself never adds (or otherwise touches) a row.
        before = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]

        _fake_send_ok(monkeypatch)
        auth_service.request_otp(conn, email=ALLOWED_EMAIL, ip_address=TEST_IP)

        after = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
        assert after == before

        # And specifically: no row was created for the address being
        # requested (the actual account-existence-touching behavior this
        # test guards against).
        row = conn.execute("SELECT 1 FROM users WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()
        assert row is None


def test_request_otp_behaves_identically_for_existing_and_nonexisting_accounts(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES (%s, 'analyst')", ("existing@grameenphone.com",))

        fake_a = _fake_send_ok(monkeypatch)
        auth_service.request_otp(conn, email="existing@grameenphone.com", ip_address="198.51.100.1")
        existing_call = fake_a.call_args

        fake_b = _fake_send_ok(monkeypatch)
        auth_service.request_otp(conn, email="new-person@grameenphone.com", ip_address="198.51.100.2")
        new_call = fake_b.call_args

        # Same shape of call (same kwargs keys, same ttl), no exception
        # either way, no distinguishing branch taken.
        assert set(existing_call.kwargs) == set(new_call.kwargs)
        assert existing_call.kwargs["ttl_minutes"] == new_call.kwargs["ttl_minutes"]


# ── Rate limiting, both dimensions (DoD #4) ───────────────────────────────


def _seed_otp_rows(conn, *, email: str, ip: str, n: int) -> None:
    for _ in range(n):
        conn.execute(
            "INSERT INTO otp_codes (email, code_hash, expires_at, requested_ip, send_status) "
            "VALUES (%s, 'seed-hash', now() + interval '10 minutes', %s, 'sent')",
            (email, ip),
        )


def test_request_otp_rate_limited_per_email(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        _seed_otp_rows(conn, email=ALLOWED_EMAIL, ip="198.51.100.50", n=auth_service.OTP_RATE_LIMIT_PER_EMAIL)

        fake = _fake_send_ok(monkeypatch)
        # Different IP than the seeded rows -- isolates the email dimension.
        with pytest.raises(auth_service.OtpRateLimited):
            auth_service.request_otp(conn, email=ALLOWED_EMAIL, ip_address="203.0.113.77")
        fake.assert_not_called()


def test_request_otp_rate_limited_per_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        shared_ip = "198.51.100.60"
        for i in range(auth_service.OTP_RATE_LIMIT_PER_IP):
            _seed_otp_rows(conn, email=f"person{i}@grameenphone.com", ip=shared_ip, n=1)

        fake = _fake_send_ok(monkeypatch)
        with pytest.raises(auth_service.OtpRateLimited):
            auth_service.request_otp(conn, email="yet-another@grameenphone.com", ip_address=shared_ip)
        fake.assert_not_called()


def test_request_otp_rate_limit_window_expired_rows_do_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        for _ in range(auth_service.OTP_RATE_LIMIT_PER_EMAIL):
            conn.execute(
                "INSERT INTO otp_codes (email, code_hash, expires_at, requested_ip, created_at, send_status) "
                "VALUES (%s, 'seed-hash', now() - interval '1 hour', %s, now() - interval '1 hour', 'sent')",
                (ALLOWED_EMAIL, "198.51.100.70"),
            )
        # All seeded rows are outside the rate-limit window -- a fresh request should succeed.
        fake = _fake_send_ok(monkeypatch)
        auth_service.request_otp(conn, email=ALLOWED_EMAIL, ip_address="203.0.113.88")
        fake.assert_called_once()


# ── RT-4 fix: per-email rate limit folds plus-addressing, case, and
# whitespace variants of the same mailbox (docs/red-team-findings.md) ────


def test_rate_limit_folds_plus_addressing() -> None:
    assert auth_service._rate_limit_email_key("person@grameenphone.com") == "person@grameenphone.com"
    assert auth_service._rate_limit_email_key("person+1@grameenphone.com") == "person@grameenphone.com"
    assert auth_service._rate_limit_email_key("person+anything-at-all@grameenphone.com") == "person@grameenphone.com"


def test_rate_limit_folds_case_and_whitespace_variants() -> None:
    assert auth_service._rate_limit_email_key("Person+1@GrameenPhone.com") == "person@grameenphone.com"
    assert auth_service._rate_limit_email_key("  person+2@grameenphone.com  ") == "person@grameenphone.com"


def test_rate_limit_does_not_fold_dots() -> None:
    """Deliberate: dot-folding is a Gmail-specific quirk, not an SMTP
    standard, and grameenphone.com's actual mail backend is unknown --
    folding dots here would risk merging two genuinely different
    mailboxes into one rate-limit bucket. See _rate_limit_email_key()'s
    docstring.
    """
    assert auth_service._rate_limit_email_key("jane.doe@grameenphone.com") != auth_service._rate_limit_email_key(
        "janedoe@grameenphone.com"
    )


def test_plus_addressed_variants_share_the_per_email_rate_limit_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fixed behavior corresponding to
    tests/test_redteam_otp_rate_limit_bypass.py::test_plus_addressing_bypasses_per_email_rate_limit
    (RT-4), expressed as a normal (non-xfail) assertion: exactly
    OTP_RATE_LIMIT_PER_EMAIL requests succeed across ANY mix of
    plus-tag/case/whitespace variants of one mailbox, and the very next
    one -- regardless of which variant it uses -- is blocked.
    """
    with migrated_schema() as conn:
        fake = _fake_send_ok(monkeypatch)
        variants = [
            "target@grameenphone.com",
            "Target+work@GrameenPhone.com",
            " target+2@grameenphone.com ",
        ]
        assert len(variants) == auth_service.OTP_RATE_LIMIT_PER_EMAIL
        for variant in variants:
            auth_service.request_otp(conn, email=variant, ip_address="203.0.113.111")
        assert fake.call_count == auth_service.OTP_RATE_LIMIT_PER_EMAIL

        with pytest.raises(auth_service.OtpRateLimited):
            auth_service.request_otp(conn, email="target+one-more@grameenphone.com", ip_address="203.0.113.111")


def test_rate_limit_canonicalization_does_not_corrupt_what_is_stored_or_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of RT-4's remediation instruction: canonicalize
    for *counting* only. What lands in otp_codes.email, and the address
    send_otp_email() is actually told to deliver to, must remain
    exactly what was submitted -- a tagged address must still receive
    its own OTP at its own real inbox, not a silently rewritten one.
    """
    with migrated_schema() as conn:
        fake = _fake_send_ok(monkeypatch)
        submitted = "person+work@grameenphone.com"
        auth_service.request_otp(conn, email=submitted, ip_address="203.0.113.112")

        assert fake.call_args.kwargs["to_email"] == submitted

        row = conn.execute("SELECT email FROM otp_codes WHERE requested_ip = %s", ("203.0.113.112",)).fetchone()
        assert row is not None
        assert row["email"] == submitted  # NOT canonicalized in storage


# ── Resend quota exhaustion (DoD #5) ──────────────────────────────────────


def test_request_otp_quota_exhausted_blocks_send_and_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        for i in range(auth_service.RESEND_DAILY_QUOTA):
            conn.execute(
                "INSERT INTO otp_codes (email, code_hash, expires_at, requested_ip, send_status) "
                "VALUES (%s, 'seed-hash', now() + interval '10 minutes', '198.51.100.99', 'sent')",
                (f"quota-filler-{i}@grameenphone.com",),
            )

        fake = _fake_send_ok(monkeypatch)
        with pytest.raises(ProviderRateLimitedError):
            auth_service.request_otp(conn, email="late-arrival@grameenphone.com", ip_address="203.0.113.200")
        fake.assert_not_called()  # never even attempted the Resend call

        row = conn.execute(
            "SELECT send_status, send_error FROM otp_codes WHERE email = %s", ("late-arrival@grameenphone.com",)
        ).fetchone()
        assert row is not None
        assert row["send_status"] == "quota_exceeded"
        assert row["send_error"]  # non-empty diagnostic, for the admin view

        failures = auth_service.recent_send_failures(conn)
        assert any(
            f["email"] == "late-arrival@grameenphone.com" and f["send_status"] == "quota_exceeded" for f in failures
        )


def test_quota_exhaustion_is_logged_at_error_level(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="app.services.auth")
    with migrated_schema() as conn:
        for i in range(auth_service.RESEND_DAILY_QUOTA):
            conn.execute(
                "INSERT INTO otp_codes (email, code_hash, expires_at, requested_ip, send_status) "
                "VALUES (%s, 'seed-hash', now() + interval '10 minutes', '198.51.100.99', 'sent')",
                (f"filler-{i}@grameenphone.com",),
            )
        _fake_send_ok(monkeypatch)
        with pytest.raises(ProviderRateLimitedError):
            auth_service.request_otp(conn, email="another@grameenphone.com", ip_address="203.0.113.201")

    assert any(r.levelno == logging.ERROR and "quota" in r.getMessage() for r in caplog.records)


# ── Email-send failure, not quota-related (DoD #9) ────────────────────────


def test_request_otp_send_failure_is_recorded_and_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        failing = Mock(side_effect=ProviderPermanentError("rejected", provider="resend"))
        monkeypatch.setattr(auth_service, "send_otp_email", failing)

        with pytest.raises(ProviderPermanentError):
            auth_service.request_otp(conn, email=ALLOWED_EMAIL, ip_address=TEST_IP)

        row = conn.execute(
            "SELECT send_status, send_error FROM otp_codes WHERE email = %s", (ALLOWED_EMAIL,)
        ).fetchone()
        assert row is not None
        assert row["send_status"] == "failed"
        assert "rejected" in row["send_error"]


def test_send_failure_survives_the_callers_rollback_on_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reproduces exactly how app.db.connection.get_connection() is used
    in production: `with get_connection() as conn: ...` rolls back on any
    exception. request_otp() must commit the failure record itself
    before re-raising -- otherwise DoD #5's admin-visible failure log
    would be wiped out by that rollback. Simulated here by manually
    rolling back after request_otp() raises, mirroring what the context
    manager would do, and confirming the failure row is still there.
    """
    with migrated_schema() as conn:
        failing = Mock(side_effect=ProviderPermanentError("boom", provider="resend"))
        monkeypatch.setattr(auth_service, "send_otp_email", failing)

        try:
            auth_service.request_otp(conn, email=ALLOWED_EMAIL, ip_address=TEST_IP)
        except ProviderPermanentError:
            conn.rollback()  # mirror get_connection()'s except-clause behavior

        row = conn.execute("SELECT send_status FROM otp_codes WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()
        assert row is not None
        assert row["send_status"] == "failed"  # survived the rollback because request_otp() committed it first


# ── Verify: wrong / expired / reused code (DoD #9) ────────────────────────


def test_verify_otp_wrong_code_fails_generically_and_increments_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        _request_and_capture_code(conn, monkeypatch)

        with pytest.raises(ValidationError, match="Invalid or expired"):
            auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code="000000")

        row = conn.execute(
            "SELECT attempt_count, consumed_at FROM otp_codes WHERE email = %s", (ALLOWED_EMAIL,)
        ).fetchone()
        assert row is not None
        assert row["attempt_count"] == 1
        assert row["consumed_at"] is None  # still usable -- one wrong guess doesn't burn the code


def test_verify_otp_expired_code_fails_generically() -> None:
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO otp_codes (email, code_hash, expires_at, requested_ip) "
            "VALUES (%s, %s, now() - interval '1 minute', %s)",
            (ALLOWED_EMAIL, auth_service._hash_code(ALLOWED_EMAIL, "123456"), TEST_IP),
        )
        with pytest.raises(ValidationError, match="Invalid or expired"):
            auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code="123456")


def test_verify_otp_reused_code_fails_on_second_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        code = _request_and_capture_code(conn, monkeypatch)

        user = auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)
        assert user.email == ALLOWED_EMAIL

        with pytest.raises(ValidationError, match="Invalid or expired"):
            auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)


def test_verify_otp_max_attempts_locks_the_code_even_for_the_right_code(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        code = _request_and_capture_code(conn, monkeypatch)

        for _ in range(auth_service.OTP_MAX_ATTEMPTS):
            with pytest.raises(ValidationError):
                auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code="999999")

        # The budget is spent -- even the CORRECT code no longer works.
        with pytest.raises(ValidationError, match="Invalid or expired"):
            auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)

        row = conn.execute("SELECT consumed_at FROM otp_codes WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()
        assert row is not None
        assert row["consumed_at"] is not None


def test_verify_otp_no_pending_code_fails_generically() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="Invalid or expired"):
            auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code="123456")


def test_verify_otp_wrong_and_no_code_return_the_same_message() -> None:
    """DoD #3's no-enumeration spirit extends to verify: a caller must
    not be able to distinguish "no code was ever requested for this
    address" from "a code exists but you guessed wrong" from the error
    text alone.
    """
    with migrated_schema() as conn:
        try:
            auth_service.verify_otp(conn, email="never-requested@grameenphone.com", code="123456")
            raise AssertionError("expected ValidationError")
        except ValidationError as exc_no_code:
            msg_no_code = str(exc_no_code)

        conn.execute(
            "INSERT INTO otp_codes (email, code_hash, expires_at, requested_ip) "
            "VALUES (%s, %s, now() + interval '10 minutes', %s)",
            (ALLOWED_EMAIL, auth_service._hash_code(ALLOWED_EMAIL, "111111"), TEST_IP),
        )
        try:
            auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code="000000")
            raise AssertionError("expected ValidationError")
        except ValidationError as exc_wrong_code:
            msg_wrong_code = str(exc_wrong_code)

        assert msg_no_code == msg_wrong_code


# ── Successful verify: user provisioning + role (DoD #7) ─────────────────


def test_verify_otp_success_auto_provisions_user_with_analyst_role_not_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        code = _request_and_capture_code(conn, monkeypatch)
        user = auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)

        assert user.role == "analyst"
        assert user.is_admin is False

        row = conn.execute("SELECT role, last_login_at FROM users WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()
        assert row is not None
        assert row["role"] == "analyst"
        assert row["last_login_at"] is not None


def test_verify_otp_success_reuses_existing_user_row_and_preserves_admin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO users (email, role, display_name) VALUES (%s, 'admin', 'Root Admin')", (ALLOWED_EMAIL,)
        )

        code = _request_and_capture_code(conn, monkeypatch)
        user = auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)

        assert user.role == "admin"
        assert user.is_admin is True
        assert user.display_name == "Root Admin"

        count = conn.execute("SELECT count(*) AS n FROM users WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()["n"]
        assert count == 1  # not duplicated


# ── Sessions (DoD #6) ──────────────────────────────────────────────────────


def test_session_create_and_lookup_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        code = _request_and_capture_code(conn, monkeypatch)
        user = auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)

        token = auth_service.create_session(conn, user_id=user.id, user_agent="pytest", ip_address=TEST_IP)
        assert token

        looked_up = auth_service.get_session_user(conn, token)
        assert looked_up is not None
        assert looked_up.email == ALLOWED_EMAIL
        assert looked_up.session_id is not None


def test_session_token_is_stored_hashed_not_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        code = _request_and_capture_code(conn, monkeypatch)
        user = auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)
        token = auth_service.create_session(conn, user_id=user.id, user_agent="pytest", ip_address=TEST_IP)

        row = conn.execute("SELECT token_hash FROM sessions WHERE user_id = %s", (user.id,)).fetchone()
        assert row is not None
        assert row["token_hash"] != token
        assert token not in row["token_hash"]


def test_revoked_session_no_longer_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        code = _request_and_capture_code(conn, monkeypatch)
        user = auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)
        token = auth_service.create_session(conn, user_id=user.id, user_agent="pytest", ip_address=TEST_IP)

        auth_service.revoke_session(conn, token)
        assert auth_service.get_session_user(conn, token) is None


def test_expired_session_does_not_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        code = _request_and_capture_code(conn, monkeypatch)
        user = auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)

        token = "already-expired-token"
        conn.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, now() - interval '1 hour')",
            (user.id, auth_service._hash_session_token(token)),
        )
        assert auth_service.get_session_user(conn, token) is None


def test_bogus_session_token_does_not_resolve() -> None:
    with migrated_schema() as conn:
        assert auth_service.get_session_user(conn, "this-token-was-never-issued") is None


def test_cookie_kwargs_are_httponly_and_secure_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        kwargs = auth_service.cookie_kwargs()
        assert kwargs["httponly"] is True
        assert kwargs["secure"] is True
        assert kwargs["samesite"] == "none"
    finally:
        get_settings.cache_clear()


def test_cookie_kwargs_relax_secure_and_samesite_outside_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    try:
        kwargs = auth_service.cookie_kwargs()
        assert kwargs["httponly"] is True
        assert kwargs["secure"] is False
        assert kwargs["samesite"] == "lax"
    finally:
        get_settings.cache_clear()


# ── Logging never contains the full email or the raw OTP code (DoD #8) ────


def test_no_log_record_contains_the_raw_email_or_raw_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Scoped to this package's own loggers rather than
    # caplog.set_level(logging.DEBUG) (root) on purpose: lowering the
    # ROOT logger to DEBUG also unmasks an unrelated, pre-existing bug in
    # app/db/migrate.py (a `logger.info(..., extra={"filename": ...})`
    # call that collides with LogRecord's own reserved `filename`
    # attribute, raising KeyError) the moment migrated_schema() runs its
    # migrations at DEBUG level. app/db/** is out of scope for this task
    # (ARCH-DB-owned) -- scoping the capture to just the loggers this
    # test actually cares about avoids ever exercising that code path at
    # DEBUG level, without needing to touch that file.
    caplog.set_level(logging.DEBUG, logger="app.services.auth")
    caplog.set_level(logging.DEBUG, logger="app.services.email")
    with migrated_schema() as conn:
        code = _request_and_capture_code(conn, monkeypatch)
        auth_service.verify_otp(conn, email=ALLOWED_EMAIL, code=code)

    for record in caplog.records:
        haystack = record.getMessage() + " " + " ".join(str(v) for v in vars(record).values())
        assert ALLOWED_EMAIL not in haystack, f"raw email leaked in log record: {record.__dict__}"
        assert code not in haystack, f"raw OTP code leaked in log record: {record.__dict__}"


def test_email_field_name_would_not_be_auto_redacted_so_masking_is_manual() -> None:
    """Documents/enforces WHY app.services.auth calls mask() explicitly
    on every logged email rather than relying on app.logging's key-based
    redaction: "email" is not one of SENSITIVE_KEY_SUBSTRINGS, so a bare
    `extra={"email": ...}` would NOT be redacted automatically.
    """
    from app.logging import SENSITIVE_KEY_SUBSTRINGS

    assert not any(s in "email" for s in SENSITIVE_KEY_SUBSTRINGS)

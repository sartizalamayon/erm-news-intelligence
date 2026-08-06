"""OTP-only authentication, sessions, and role resolution (ADR §9.3,
frontend-contract.md §3.0/§2, migrations/0005_auth.sql).

No passwords anywhere, by design: the design mockup ships a password
field (ADR §5's "blocking contradiction"), but the owner ruled OTP-only
(ADR §9.3) and 0005_auth.sql dropped `password_hash` on purpose. Role is
never client-supplied -- the mock's login-time role picker is flagged in
ADR §5 as a security defect, not a requirement, and is not built here.

Session mechanism -- opaque server-side token, not a JWT
------------------------------------------------------------------------
`sessions.token_hash` / `.revoked_at` / `.expires_at` (0005_auth.sql) are
exactly the shape of an opaque-token session store, not a JWT design --
a JWT needs no `token_hash` column at all (you'd verify a signature, not
look one up). This module follows the schema it was given: a
cryptographically random 256-bit token (`secrets.token_urlsafe(32)`) is
handed to the browser as a cookie; only its SHA-256 hash is ever stored,
so a leaked database dump does not hand out working session tokens. This
is chosen over a JWT specifically *because* JWTs cannot be revoked
without an extra blocklist (reinventing this exact table) -- and
DoD #6 requires real server-side invalidation ("log out", role changes,
an admin killing a compromised session), which an opaque, DB-backed
token gives for free via `revoked_at`. Fixed TTL, no sliding refresh
(see SESSION_TTL_HOURS below) -- simplest correct thing; a sliding
window is a reasonable follow-up, not added here to avoid a second time
source to reason about under time pressure.

OTP parameters and why
------------------------------------------------------------------------
* 6 digits, `secrets.randbelow` (never `random` -- DoD #2) -- the
  standard OTP length; 10^6 space combined with a 5-attempt lockout
  (OTP_MAX_ATTEMPTS) below gives a guessing attacker at most 5 chances
  in 1,000,000 per code, and the code is dead after that regardless.
* 10 minute expiry -- long enough to survive a slow corporate mail
  filter (the deliverability risk flagged in ADR §9.3 and in this
  package's task report) without the user needing to restart the whole
  flow, short enough that a leaked/forwarded code has a narrow window.
* Single use, enforced by `otp_codes.consumed_at` -- verify_otp() only
  ever matches rows where `consumed_at IS NULL` (the exact query
  `idx_otp_codes_email_active` was built for -- 0005_auth.sql's own
  comment) and marks the row consumed inside the same transaction as
  the success path, so a resubmitted correct code after a first success
  finds no active row and fails generically, identical to a wrong code.
* Code storage: HMAC-SHA256 keyed by `app.config.Settings.secret_key`
  (the same Fernet-adjacent secret already used for provider-key
  encryption, app/db/crypto.py) rather than a bare hash. A 6-digit code
  is low-entropy; keying the hash means a stolen `otp_codes` table alone
  is not enough to offline-brute-force live codes -- the attacker also
  needs SECRET_KEY. Comparison is `hmac.compare_digest` (DoD #2), never
  `==`.

Rate limiting and Resend's 100/day ceiling
------------------------------------------------------------------------
ADR §9.3's capacity note: 100/day is the binding Resend constraint, not
3,000/month, and per-address/per-IP rate limiting "doubles as the
protection against exhausting the daily quota -- a single abusive or
looping client must not be able to burn the org's whole day of email."
Chosen values, using the exact indexes 0005_auth.sql built for this
(`idx_otp_codes_email_created`, `idx_otp_codes_ip_created`):
  * Max 3 requests per email per rolling 15 minutes -- generous enough
    for a real user who fat-fingers a code and asks for a fresh one a
    couple of times, tight enough that a runaway retry loop against one
    address is stopped within its first few requests, not its first few
    hundred.
  * Max 10 requests per IP per rolling 15 minutes -- higher than the
    per-email ceiling on purpose: a shared corporate NAT/VPN egress IP
    (very plausible for @grameenphone.com staff) can legitimately
    represent many different people requesting codes around the same
    time; this dimension exists to catch a single client hammering many
    addresses from one IP, not to further squeeze normal traffic.
  * A pre-send daily quota check (RESEND_DAILY_QUOTA) additionally caps
    total sends across ALL addresses at 100/24h, so no combination of
    legitimately-distinct users can collectively exceed Resend's ceiling
    either -- see request_otp()/_daily_quota_reached().

Enumeration (DoD #3)
------------------------------------------------------------------------
request_otp() never queries the `users` table at all. Any syntactically
valid, domain-allowed address takes the exact same code path -- generate
a code, store its hash, attempt to send -- whether or not a `users` row
exists yet for it; the user is created (role defaults to 'analyst' per
0005_auth.sql's CHECK constraint) lazily, only on a *successful*
verify_otp(). There is therefore no branch anywhere in the request path
whose timing or response body could depend on account existence. The one
deliberately *visible* distinction is the domain check itself (DoD #3:
"identical... beyond the domain check itself") -- frontend-contract.md
§3.0's own `loginErr` example ("Access is limited to @grameenphone.com
addresses") is a domain-format fact, not an account-existence fact, so
surfacing it is not an enumeration leak.

Role: `admin` / `analyst`, not `admin` / `user`
------------------------------------------------------------------------
The mission brief describes roles generically as "user and admin", but
0005_auth.sql's actual CHECK constraint is
`role IN ('admin', 'analyst')`, matching frontend-contract.md's
`role === 'admin'` / "Analyst"/"Administrator" labels exactly. This
module uses the schema's real values (`'analyst'` is the default, never
client-suppliable) -- inserting anything else would fail the CHECK
constraint outright.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.db.connection import DictConnection
from app.db.settings import get_setting
from app.errors import ConfigurationError, ProviderError, ProviderRateLimitedError, ValidationError
from app.logging import get_logger, mask
from app.services.email import send_otp_email

logger = get_logger(__name__)

# ── OTP parameters (see module docstring for reasoning) ─────────────────
OTP_CODE_LENGTH = 6
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5

# ── Rate limiting (per-address / per-IP, ADR §9.3 + DoD #4) ─────────────
OTP_RATE_LIMIT_PER_EMAIL = 3
OTP_RATE_LIMIT_EMAIL_WINDOW_MINUTES = 15
OTP_RATE_LIMIT_PER_IP = 10
OTP_RATE_LIMIT_IP_WINDOW_MINUTES = 15

# ── Resend quota (ADR §9.3 capacity note: "100/day is the binding
# constraint") ───────────────────────────────────────────────────────────
RESEND_DAILY_QUOTA = 100
RESEND_QUOTA_WARN_THRESHOLD = 80  # log a warning once 80% of the day's budget is used, before the hard stop.

# ── Sessions (see module docstring for the opaque-token-vs-JWT choice) ──
SESSION_COOKIE_NAME = "erm_session"
SESSION_TTL_HOURS = 12

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OtpRateLimited(Exception):
    """Raised by request_otp() when a per-email or per-IP rate limit is
    hit. Deliberately NOT part of app.errors' AppError taxonomy -- that
    module's own docstring reserves adding a new top-level category for
    ARCH-REPO ("if a genuinely new top-level category is needed, that's
    an ARCH-REPO-level change... not a per-agent one"). This is a
    narrow, service-local control-flow signal; app.api.routers.auth
    converts it to an HTTP 429.
    """


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    email: str
    role: str
    display_name: str
    # None until a session actually exists (verify_otp()'s return value, before create_session() runs).
    session_id: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ── Email / domain helpers ───────────────────────────────────────────────


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _is_syntactically_valid(email: str) -> bool:
    return bool(_EMAIL_RE.match(email))


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1]


def is_domain_allowed(conn: DictConnection, email: str) -> bool:
    """DoD #1: server-side, config-driven -- not a hardcoded literal.
    Sourced from the `settings` table (0001_settings.sql's
    `allowed_email_domain`, seeded 'grameenphone.com'), via
    app.db.settings.get_setting(), the accessor ARCH-DB already built
    specifically for "the OTP-allowed email domain" (that module's own
    docstring). This is a runtime-admin-editable value, not an
    app.config.Settings env var -- correct, since an admin should be
    able to change the allowed domain without a redeploy, exactly what
    that table exists for.
    """
    allowed = get_setting(conn, "allowed_email_domain", "grameenphone.com").strip().lower()
    return _email_domain(email) == allowed


def _configured_domain(conn: DictConnection) -> str:
    return get_setting(conn, "allowed_email_domain", "grameenphone.com").strip().lower()


# ── OTP code generation / hashing (DoD #2) ───────────────────────────────


def generate_otp_code() -> str:
    """`secrets`, never `random` (DoD #2) -- randbelow(10**N) is uniform
    over [0, 10**N), zero-padded to a fixed-width numeric string.
    """
    return f"{secrets.randbelow(10**OTP_CODE_LENGTH):0{OTP_CODE_LENGTH}d}"


def _hash_code(email: str, code: str) -> str:
    """HMAC-SHA256 keyed by SECRET_KEY -- see module docstring's "OTP
    parameters" section for why this is keyed rather than a bare hash.
    Includes the (already-normalized) email as associated data purely
    as cheap extra hygiene against cross-address hash reuse; the real
    security property comes from the key, not this.
    """
    settings = get_settings()
    if not settings.secret_key:
        raise ConfigurationError("SECRET_KEY is not configured -- cannot hash OTP codes.")
    key = settings.secret_key.encode("utf-8")
    message = f"{email}:{code}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _hash_session_token(token: str) -> str:
    # Session tokens are 256 bits of secrets.token_urlsafe output -- not
    # brute-forceable at any practical rate, unlike a 6-digit OTP -- so
    # a plain (unkeyed) SHA-256 is enough here; keying it would only add
    # a SECRET_KEY dependency to session *verification*, coupling it to
    # Fernet-key rotation concerns for no real security benefit.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Rate limiting / quota ────────────────────────────────────────────────


def _enforce_rate_limits(conn: DictConnection, *, email: str, ip_address: str) -> None:
    email_row = conn.execute(
        "SELECT count(*) AS n FROM otp_codes WHERE email = %s AND created_at > now() - (%s || ' minutes')::interval",
        (email, OTP_RATE_LIMIT_EMAIL_WINDOW_MINUTES),
    ).fetchone()
    if email_row is not None and email_row["n"] >= OTP_RATE_LIMIT_PER_EMAIL:
        raise OtpRateLimited("Too many codes requested for this address. Try again later.")

    if ip_address:
        ip_row = conn.execute(
            "SELECT count(*) AS n FROM otp_codes WHERE requested_ip = %s "
            "AND created_at > now() - (%s || ' minutes')::interval",
            (ip_address, OTP_RATE_LIMIT_IP_WINDOW_MINUTES),
        ).fetchone()
        if ip_row is not None and ip_row["n"] >= OTP_RATE_LIMIT_PER_IP:
            raise OtpRateLimited("Too many codes requested from this network. Try again later.")


def _sent_last_24h(conn: DictConnection) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM otp_codes WHERE send_status = 'sent' AND created_at > now() - interval '24 hours'"
    ).fetchone()
    return int(row["n"]) if row else 0


def _daily_quota_reached(conn: DictConnection) -> bool:
    sent = _sent_last_24h(conn)
    if sent >= RESEND_QUOTA_WARN_THRESHOLD:
        logger.warning("otp_quota_approaching", extra={"sent_last_24h": sent, "quota": RESEND_DAILY_QUOTA})
    return sent >= RESEND_DAILY_QUOTA


# ── Request / verify ──────────────────────────────────────────────────────


def request_otp(conn: DictConnection, *, email: str, ip_address: str) -> None:
    """Generate, store, and email one OTP code. See module docstring for
    the full reasoning (enumeration, rate limiting, quota).

    Raises:
        ValidationError     -- malformed email, or domain not allowed.
        OtpRateLimited       -- per-email or per-IP rate limit hit.
        ProviderRateLimitedError / ProviderPermanentError
                              -- Resend quota exhausted, or the send
                                 failed (network, or Resend rejected it).
    Returns None on success (a code was generated, stored, and Resend
    accepted the send).
    """
    email = normalize_email(email)
    if not _is_syntactically_valid(email):
        raise ValidationError("Enter a valid work email address.")

    if not is_domain_allowed(conn, email):
        raise ValidationError(f"Access is limited to @{_configured_domain(conn)} addresses.")

    _enforce_rate_limits(conn, email=email, ip_address=ip_address)

    if _daily_quota_reached(conn):
        _record_blocked_send(conn, email=email, ip_address=ip_address, detail="daily Resend quota reached")
        logger.error("otp_quota_exhausted", extra={"email_masked": mask(email), "quota": RESEND_DAILY_QUOTA})
        raise ProviderRateLimitedError("Daily email quota exhausted", provider="resend")

    code = generate_otp_code()
    code_hash = _hash_code(email, code)
    row = conn.execute(
        """
        INSERT INTO otp_codes (email, code_hash, purpose, expires_at, requested_ip, send_status)
        VALUES (%s, %s, 'login', now() + (%s || ' minutes')::interval, %s, 'pending')
        RETURNING id
        """,
        (email, code_hash, OTP_TTL_MINUTES, ip_address),
    ).fetchone()
    assert row is not None
    row_id = row["id"]
    # Durability point: even if the process dies mid-send, this row
    # exists and is queryable (as 'pending') rather than the whole
    # attempt vanishing on an implicit rollback.
    conn.commit()

    try:
        send_otp_email(to_email=email, code=code, ttl_minutes=OTP_TTL_MINUTES)
    except ProviderError as exc:
        status = "quota_exceeded" if isinstance(exc, ProviderRateLimitedError) else "failed"
        conn.execute(
            "UPDATE otp_codes SET send_status = %s, send_error = %s WHERE id = %s",
            (status, str(exc)[:500], row_id),
        )
        # Commit BEFORE re-raising: this function's caller almost always
        # runs inside `with get_connection() as conn: ...`
        # (app.db.connection), whose context manager rolls back on any
        # exception. Without this explicit commit, the very failure
        # record DoD #5 requires ("surfaced to admins") would be wiped
        # out by the rollback triggered by propagating this exception.
        conn.commit()
        logger.error("otp_send_failed", extra={"email_masked": mask(email), "send_status": status})
        raise

    conn.execute("UPDATE otp_codes SET send_status = 'sent' WHERE id = %s", (row_id,))
    conn.commit()


def _record_blocked_send(conn: DictConnection, *, email: str, ip_address: str, detail: str) -> None:
    """Persist a quota-exceeded attempt that never reached Resend at all
    (the pre-send daily-cap check in request_otp() tripped). Stores the
    hash of a code that was never generated for real use -- one that is
    already-expired at insert time -- so this row can never be used to
    complete a login, while still giving the admin-visible failure log
    (DoD #5) and the rate-limit counters (this row still counts toward
    idx_otp_codes_email_created / idx_otp_codes_ip_created, correctly
    treating a blocked attempt as still an attempt) a real row to see.
    """
    dummy_code = generate_otp_code()
    code_hash = _hash_code(email, dummy_code)
    conn.execute(
        """
        INSERT INTO otp_codes (email, code_hash, purpose, expires_at, requested_ip, send_status, send_error)
        VALUES (%s, %s, 'login', now(), %s, 'quota_exceeded', %s)
        """,
        (email, code_hash, ip_address, detail[:500]),
    )
    conn.commit()


def recent_send_failures(conn: DictConnection, limit: int = 50) -> list[dict[str, object]]:
    """Backs the admin-only GET /auth/otp-failures endpoint (DoD #5:
    "surfaced to admins"). Uses idx_otp_codes_send_status_failures
    (migrations/0008_otp_send_status.sql).
    """
    rows = conn.execute(
        """
        SELECT email, send_status, send_error, created_at
        FROM otp_codes
        WHERE send_status <> 'sent'
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


_GENERIC_VERIFY_ERROR = "Invalid or expired verification code."


def verify_otp(conn: DictConnection, *, email: str, code: str) -> AuthenticatedUser:
    """Check a submitted code against the single still-live OTP for that
    address (idx_otp_codes_email_active's exact query), consume it on
    success, and lazily get-or-create the `users` row.

    Every failure path (no active code, expired, already consumed,
    wrong code, domain no longer allowed) raises the SAME
    ValidationError with the SAME message -- deliberately generic so a
    caller cannot distinguish "wrong code" from "no such pending login"
    from "already used", which would otherwise leak information about
    whether a request was ever made for that address.
    """
    email = normalize_email(email)

    # Defense in depth: request_otp() already domain-gates issuance, so
    # in the normal case this can only find a row for an allowed domain.
    # Re-checking here costs one cheap settings lookup and closes the
    # edge case where the allowed domain changes between request and
    # verify.
    if not is_domain_allowed(conn, email):
        raise ValidationError(_GENERIC_VERIFY_ERROR)

    row = conn.execute(
        """
        SELECT id, code_hash, attempt_count
        FROM otp_codes
        WHERE email = %s AND consumed_at IS NULL AND expires_at > now()
        ORDER BY created_at DESC
        LIMIT 1
        FOR UPDATE
        """,
        (email,),
    ).fetchone()

    if row is None:
        raise ValidationError(_GENERIC_VERIFY_ERROR)

    submitted_hash = _hash_code(email, code)
    if not hmac.compare_digest(submitted_hash, row["code_hash"]):
        new_attempts = row["attempt_count"] + 1
        if new_attempts >= OTP_MAX_ATTEMPTS:
            # Burn the code outright once the attempt budget is spent --
            # DoD #2's "single use" plus this lockout together bound a
            # guessing attacker to OTP_MAX_ATTEMPTS tries in 10**OTP_CODE_LENGTH,
            # never an unlimited number of tries against one code.
            conn.execute(
                "UPDATE otp_codes SET attempt_count = %s, consumed_at = now() WHERE id = %s",
                (new_attempts, row["id"]),
            )
        else:
            conn.execute("UPDATE otp_codes SET attempt_count = %s WHERE id = %s", (new_attempts, row["id"]))
        # Same reasoning as request_otp()'s failure path: commit before
        # raising, so the attempt-count increment (the actual
        # brute-force defense) survives the caller's rollback-on-exception.
        conn.commit()
        raise ValidationError(_GENERIC_VERIFY_ERROR)

    conn.execute("UPDATE otp_codes SET consumed_at = now() WHERE id = %s", (row["id"],))

    user_row = _get_or_create_user(conn, email)
    conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (user_row["id"],))
    # No explicit commit here: this is the success path, and the caller
    # (app.api.routers.auth.verify_otp, inside `with get_connection()`)
    # goes on to call create_session() using this SAME connection --
    # code-consumption + user upsert + session creation commit together,
    # atomically, when that outer `with` block exits cleanly. That is
    # the correct behavior: we never want a session issued without the
    # code being durably marked consumed, or vice versa.

    return AuthenticatedUser(
        id=user_row["id"],
        email=user_row["email"],
        role=user_row["role"],
        display_name=user_row["display_name"],
    )


def _get_or_create_user(conn: DictConnection, email: str) -> dict[str, Any]:
    row = conn.execute("SELECT id, email, role, display_name FROM users WHERE email = %s", (email,)).fetchone()
    if row is not None:
        return dict(row)
    row = conn.execute(
        "INSERT INTO users (email) VALUES (%s) RETURNING id, email, role, display_name",
        (email,),
    ).fetchone()
    assert row is not None
    logger.info("user_auto_provisioned", extra={"email_masked": mask(email)})
    return dict(row)


# ── Sessions ──────────────────────────────────────────────────────────────


def create_session(conn: DictConnection, *, user_id: int, user_agent: str, ip_address: str) -> str:
    """Returns the RAW token (to hand to the browser as a cookie) --
    only its hash is persisted, matching 0005_auth.sql's
    "never stores a secret in recoverable form" design.
    """
    token = secrets.token_urlsafe(32)
    token_hash = _hash_session_token(token)
    conn.execute(
        """
        INSERT INTO sessions (user_id, token_hash, expires_at, user_agent, ip_address)
        VALUES (%s, %s, now() + (%s || ' hours')::interval, %s, %s)
        """,
        (user_id, token_hash, SESSION_TTL_HOURS, user_agent[:500], ip_address),
    )
    return token


def get_session_user(conn: DictConnection, token: str) -> AuthenticatedUser | None:
    token_hash = _hash_session_token(token)
    row = conn.execute(
        """
        SELECT u.id AS id, u.email AS email, u.role AS role, u.display_name AS display_name, s.id AS session_id
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()
        """,
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    return AuthenticatedUser(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        display_name=row["display_name"],
        session_id=row["session_id"],
    )


def revoke_session(conn: DictConnection, token: str) -> None:
    token_hash = _hash_session_token(token)
    conn.execute("UPDATE sessions SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL", (token_hash,))


def cookie_kwargs() -> dict[str, object]:
    """Cookie attributes for the session cookie (DoD #6: secure,
    httpOnly, SameSite). `secure`/`samesite` are derived from
    `app.config.Settings.app_env` rather than hardcoded: ADR §1 puts the
    frontend (Render Static Site) and API (Render web service) on
    different subdomains, which is cross-site for cookie purposes and
    requires `SameSite=None; Secure` in production/staging. Local dev
    typically runs both over plain http on different localhost ports --
    same-site for SameSite purposes (browsers key "site" on eTLD+1,
    ignoring port), where `Secure` would silently drop the cookie over
    http, so `Lax` + non-secure is used there instead.
    """
    settings = get_settings()
    production_like = settings.app_env in ("production", "staging")
    return {
        "key": SESSION_COOKIE_NAME,
        "httponly": True,
        "secure": production_like,
        "samesite": "none" if production_like else "lax",
        "max_age": SESSION_TTL_HOURS * 3600,
        "path": "/",
    }

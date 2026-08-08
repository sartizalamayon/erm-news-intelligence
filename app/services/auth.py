"""Authentication, sessions, and role resolution.

OWNER-RULED MODEL CHANGE (supersedes ADR §9.3's "OTP only"):
sending an OTP on every login needs a verified sending domain, which
grameenphone.com does not have and the owner has ruled out getting DNS
access for. New model, one email per account for its whole lifetime:
  * Signup / password reset: request an OTP, then POST it together with
    a new password to /auth/password/set (one call -- proves ownership
    and sets the password atomically). migrations/0010_password_auth.sql
    adds `users.password_hash` (PHC-encoded Argon2id; '' = no password
    set yet -- the single source of truth for "can this account sign in
    yet", covering both a brand-new account and the seeded bootstrap
    admin, migrations/0009_seed_admin.sql, identically).
  * Every sign-in after that: POST /auth/login, email + password, no
    email sent. See login()/verify_password() below.
  * AUTH-V2 addendum: an already-authenticated user rotating their own
    password (no OTP involved) is POST /auth/password/change. Still
    requires the CURRENT password -- a stolen session cookie alone must
    not be enough to permanently take over the account -- and is
    rate-limited per user AND per IP (password_change_attempts,
    migrations/0011_change_password_attempts.sql) for the same reason
    login() is: asking for the current password necessarily makes
    "right guess" and "wrong guess" distinguishable, i.e. a guessing
    oracle, unless bounded. See change_password() below.
Everything OTP already had -- secrets-based generation, HMAC-keyed hash
at rest, hmac.compare_digest, short expiry, single use, per-email AND
per-IP rate limiting (with plus-addressing canonicalization) -- is
unchanged; only what a verified OTP *unlocks* changed. Role is never
client-supplied -- the mock's login-time role picker is flagged in
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
* Single use, enforced by `otp_codes.consumed_at` -- _consume_active_otp()
  (called by set_password()) only ever matches rows where
  `consumed_at IS NULL` (the exact query `idx_otp_codes_email_active`
  was built for -- 0005_auth.sql's own comment) and marks the row
  consumed inside the same transaction as the success path, so a
  resubmitted correct code after a first success finds no active row
  and fails generically, identical to a wrong code.
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
set_password(). There is therefore no branch anywhere in the request path
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
from functools import lru_cache
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

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

# ── Passwords ────────────────────────────────────────────────────────────
# Argon2id (OWASP's recommended default over bcrypt) via `argon2-cffi`
# (pyproject.toml, ARCH-REPO commit a5def6c -- added specifically for
# this task once flagged as a missing dependency). `PasswordHasher.hash`/
# `.verify` produce/consume the standard self-describing PHC string
# (algorithm, version, cost params, salt, hash all in one value) --
# nothing else needs to be stored alongside `users.password_hash`.
ARGON2_TIME_COST = 3
ARGON2_MEMORY_COST_KIB = 64 * 1024  # 64 MiB
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16

_ARGON2_HASHER = PasswordHasher(
    time_cost=ARGON2_TIME_COST,
    memory_cost=ARGON2_MEMORY_COST_KIB,
    parallelism=ARGON2_PARALLELISM,
    hash_len=ARGON2_HASH_LEN,
    salt_len=ARGON2_SALT_LEN,
)

PASSWORD_MIN_LENGTH = 12  # length over complexity rules, current NIST-ish guidance.
# Curated blocklist of extremely common values, not a full breached-password
# corpus (e.g. HaveIBeenPwned) -- a reasonable follow-up, not built here.
_WEAK_PASSWORDS = frozenset(
    {
        "password", "password1", "password123", "12345678", "123456789", "1234567890",
        "qwerty123", "qwertyuiop", "letmein123", "welcome123", "changeme", "changeme123",
        "admin1234", "administrator", "iloveyou1", "correcthorsebatterystaple",
        "grameenphone", "grameenphone1", "grameenphone123", "gpcompany1", "gp123456789",
    }
)

# ── Login rate limiting (login brute force is now the primary attack
# surface -- it did not exist before password auth). Same shape as OTP's
# (idx_login_attempts_email_created / _ip_created,
# migrations/0010_password_auth.sql), and the SAME plus-addressing
# canonicalization (_rate_limit_email_key) so RT-4's fix covers this too.
LOGIN_RATE_LIMIT_PER_EMAIL = 5
LOGIN_RATE_LIMIT_EMAIL_WINDOW_MINUTES = 15
LOGIN_RATE_LIMIT_PER_IP = 20
LOGIN_RATE_LIMIT_IP_WINDOW_MINUTES = 15

# ── Change-password rate limiting (AUTH-V2 addendum). A caller here is
# already authenticated (holds a valid session), but change_password()
# still requires the CURRENT password -- which means a wrong guess and a
# right guess get distinguishably different responses, exactly the
# shape of an oracle a stolen session could brute-force the real
# password out of without this. Keyed by user_id (not email --
# the caller is already resolved to a specific account via their
# session, so there is nothing to canonicalize/plus-fold the way
# request_otp()/login() do for an attacker-supplied address) and by IP,
# same two dimensions as everywhere else in this module.
# migrations/0011_change_password_attempts.sql.
CHANGE_PASSWORD_RATE_LIMIT_PER_USER = 5
CHANGE_PASSWORD_RATE_LIMIT_PER_IP = 20
CHANGE_PASSWORD_RATE_LIMIT_WINDOW_MINUTES = 15

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


class LoginRateLimited(Exception):
    """Same reasoning as OtpRateLimited, for login()'s per-email/per-IP
    brute-force limiter (login_attempts table).
    """


class PasswordChangeRateLimited(Exception):
    """Same reasoning as OtpRateLimited/LoginRateLimited, for
    change_password()'s per-user/per-IP limiter (password_change_attempts
    table, migrations/0011_change_password_attempts.sql).
    """


@dataclass(frozen=True)
class AuthenticatedUser:
    id: int
    email: str
    role: str
    display_name: str
    # None until a session actually exists (set_password()'s/login()'s return value, before create_session() runs).
    session_id: int | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


# ── Email / domain helpers ───────────────────────────────────────────────


def normalize_email(email: str) -> str:
    return email.strip().lower()


# SQL expression computing the same canonical form as _rate_limit_email_key()
# below, applied to the `email` COLUMN so stored rows (which keep the raw,
# as-submitted address -- see that function's docstring for why) can still be
# compared against an already-canonicalized incoming value. A fixed literal,
# never interpolated with request-supplied input, so building it via an
# f-string elsewhere is not a SQL-injection concern.
_EMAIL_RATE_LIMIT_CANONICAL_SQL = (
    "regexp_replace(split_part(email, '@', 1), '\\+.*$', '') || '@' || split_part(email, '@', 2)"
)


def _rate_limit_email_key(email: str) -> str:
    """Canonical form used ONLY for OTP rate-limit counting (RT-4,
    docs/red-team-findings.md) and nowhere else -- never for what is
    stored in `otp_codes.email` / `users.email` or the address Resend
    is actually told to deliver to, both of which must stay exactly
    what the caller submitted (the OTP has to be emailed to the real
    address the user typed, tag and all).

    Folds a `+tag` local-part suffix: `person@x.com`,
    `person+1@x.com`, `person+anything@x.com` all become the same key.
    This is Gmail-style plus-addressing, also standardized as RFC 5233
    "Sieve" subaddressing and widely deployed on corporate mail via
    Postfix's `recipient_delimiter` -- common enough that a rate
    limiter must not assume it away, which is exactly how RT-4 defeated
    OTP_RATE_LIMIT_PER_EMAIL (unlimited codes to one real mailbox by
    incrementing the tag).

    Deliberately does NOT fold dots in the local part. Gmail treats
    `jane.doe@gmail.com` and `janedoe@gmail.com` as the same mailbox,
    but that is a Gmail-specific quirk, not part of the SMTP standard
    -- folding dots unconditionally for an arbitrary corporate domain
    like grameenphone.com (which may or may not run on Google
    Workspace) risks the opposite failure, wrongly conflating two
    genuinely different mailboxes into one rate-limit bucket. Case and
    surrounding whitespace are already folded by normalize_email().
    """
    email = normalize_email(email)
    if "@" not in email:
        return email
    local, domain = email.split("@", 1)
    local = local.split("+", 1)[0]
    return f"{local}@{domain}"


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


def hash_password(password: str) -> str:
    """Returns a PHC-encoded Argon2id string -- self-contained (salt +
    cost params + hash), nothing else needs to be stored alongside it.
    `PasswordHasher.hash` generates its own random salt internally
    (argon2-cffi's own os.urandom-backed default), so no separate
    `secrets.token_bytes` call is needed here.
    """
    return _ARGON2_HASHER.hash(password)


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    # Computed once, lazily (not at import), so importing this module
    # never pays Argon2's deliberate cost unless a login actually happens.
    return hash_password("dummy-constant-effort-target-never-a-real-account-password")


def verify_password(password: str, password_hash: str) -> bool:
    """Always performs one real Argon2 verify -- against `password_hash`
    if set, or a fixed dummy hash if not (no password set yet, e.g. a
    not-yet-signed-up or bootstrap-only account) -- so login()'s
    no-enumeration property extends to timing, not just the response
    body. Argon2's own verify does the actual secret comparison
    (re-derives from the embedded salt and compares in constant time,
    inside the underlying C library) -- this is not re-implemented by
    hand. `InvalidHashError` (malformed/foreign hash -- should not occur
    for anything this module itself wrote, but a corrupted row must fail
    closed, not raise) is treated identically to a wrong password.
    """
    target = password_hash if password_hash else _dummy_password_hash()
    try:
        _ARGON2_HASHER.verify(target, password)
        return bool(password_hash)
    except (VerificationError, InvalidHashError):
        return False


def _validate_password_strength(password: str, *, email: str) -> None:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValidationError(f"Password must be at least {PASSWORD_MIN_LENGTH} characters.")
    lowered = password.lower()
    if lowered in _WEAK_PASSWORDS:
        raise ValidationError("That password is too common. Choose a stronger one.")
    local_part = email.split("@", 1)[0].lower()
    if local_part and local_part in lowered:
        raise ValidationError("Password must not contain your email address.")


# ── Rate limiting / quota ────────────────────────────────────────────────


def _enforce_rate_limits(conn: DictConnection, *, email: str, ip_address: str) -> None:
    # RT-4 fix: count against the CANONICAL identity (plus-tag folded), on
    # both sides of the comparison -- the incoming email (Python-side) and
    # every stored row's `email` column (SQL-side, via the same fold
    # expressed as SQL). `otp_codes.email` itself is left untouched --
    # still the raw, as-submitted address -- so this only changes what
    # counts as "the same address" for rate-limiting, never what gets
    # stored or emailed.
    canonical_email = _rate_limit_email_key(email)
    email_row = conn.execute(
        f"SELECT count(*) AS n FROM otp_codes WHERE {_EMAIL_RATE_LIMIT_CANONICAL_SQL} = %s "
        "AND created_at > now() - (%s || ' minutes')::interval",
        (canonical_email, OTP_RATE_LIMIT_EMAIL_WINDOW_MINUTES),
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


def _enforce_login_rate_limits(conn: DictConnection, *, email: str, ip_address: str) -> None:
    """Same shape as _enforce_rate_limits() above, over login_attempts
    (migrations/0010_password_auth.sql) instead of otp_codes -- same
    plus-addressing canonicalization (RT-4's fix applies here too).
    """
    canonical_email = _rate_limit_email_key(email)
    email_row = conn.execute(
        f"SELECT count(*) AS n FROM login_attempts WHERE {_EMAIL_RATE_LIMIT_CANONICAL_SQL} = %s "
        "AND created_at > now() - (%s || ' minutes')::interval",
        (canonical_email, LOGIN_RATE_LIMIT_EMAIL_WINDOW_MINUTES),
    ).fetchone()
    if email_row is not None and email_row["n"] >= LOGIN_RATE_LIMIT_PER_EMAIL:
        raise LoginRateLimited("Too many login attempts for this address. Try again later.")

    if ip_address:
        ip_row = conn.execute(
            "SELECT count(*) AS n FROM login_attempts WHERE ip_address = %s "
            "AND created_at > now() - (%s || ' minutes')::interval",
            (ip_address, LOGIN_RATE_LIMIT_IP_WINDOW_MINUTES),
        ).fetchone()
        if ip_row is not None and ip_row["n"] >= LOGIN_RATE_LIMIT_PER_IP:
            raise LoginRateLimited("Too many login attempts from this network. Try again later.")


def _record_login_attempt(conn: DictConnection, *, email: str, ip_address: str) -> None:
    # Recorded regardless of outcome (success or failure), BEFORE the
    # password check runs, and committed immediately -- an attacker
    # short-circuiting early must still be counted, and the attempt must
    # survive even if something later in the same request raises.
    conn.execute("INSERT INTO login_attempts (email, ip_address) VALUES (%s, %s)", (email, ip_address))
    conn.commit()


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


_OTP_PURPOSES = frozenset({"signup", "password_reset"})


def request_otp(conn: DictConnection, *, email: str, ip_address: str, purpose: str = "signup") -> None:
    """Generate, store, and email one OTP code -- used for BOTH signup
    (first-ever password) and password reset (owner-ruled model change,
    see module docstring); `purpose` only affects the email copy
    (app.services.email) and the `otp_codes.purpose` column, never the
    security properties below.

    Raises:
        ValidationError     -- malformed email, or domain not allowed.
        OtpRateLimited       -- per-email or per-IP rate limit hit.
        ProviderRateLimitedError / ProviderPermanentError
                              -- provider quota exhausted, or the send
                                 failed (network, or rejected).
    Returns None on success (a code was generated, stored, and the
    email provider accepted the send).
    """
    email = normalize_email(email)
    if purpose not in _OTP_PURPOSES:
        purpose = "signup"
    if not _is_syntactically_valid(email):
        raise ValidationError("Enter a valid work email address.")

    if not is_domain_allowed(conn, email):
        raise ValidationError(f"Access is limited to @{_configured_domain(conn)} addresses.")

    _enforce_rate_limits(conn, email=email, ip_address=ip_address)

    if _daily_quota_reached(conn):
        _record_blocked_send(
            conn, email=email, ip_address=ip_address, purpose=purpose, detail="daily email quota reached"
        )
        logger.error("otp_quota_exhausted", extra={"email_masked": mask(email), "quota": RESEND_DAILY_QUOTA})
        raise ProviderRateLimitedError("Daily email quota exhausted", provider="email")

    code = generate_otp_code()
    code_hash = _hash_code(email, code)
    row = conn.execute(
        """
        INSERT INTO otp_codes (email, code_hash, purpose, expires_at, requested_ip, send_status)
        VALUES (%s, %s, %s, now() + (%s || ' minutes')::interval, %s, 'pending')
        RETURNING id
        """,
        (email, code_hash, purpose, OTP_TTL_MINUTES, ip_address),
    ).fetchone()
    assert row is not None
    row_id = row["id"]
    # Durability point: even if the process dies mid-send, this row
    # exists and is queryable (as 'pending') rather than the whole
    # attempt vanishing on an implicit rollback.
    conn.commit()

    try:
        send_otp_email(to_email=email, code=code, ttl_minutes=OTP_TTL_MINUTES, purpose=purpose)
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


def _record_blocked_send(conn: DictConnection, *, email: str, ip_address: str, purpose: str, detail: str) -> None:
    """Persist a quota-exceeded attempt that never reached the email
    provider at all (the pre-send daily-cap check in request_otp()
    tripped). Stores the hash of a code that was never generated for
    real use -- one that is already-expired at insert time -- so this
    row can never be used to complete a signup/reset, while still giving
    the admin-visible failure log (DoD #5) and the rate-limit counters
    (this row still counts toward idx_otp_codes_email_created /
    idx_otp_codes_ip_created, correctly treating a blocked attempt as
    still an attempt) a real row to see.
    """
    dummy_code = generate_otp_code()
    code_hash = _hash_code(email, dummy_code)
    conn.execute(
        """
        INSERT INTO otp_codes (email, code_hash, purpose, expires_at, requested_ip, send_status, send_error)
        VALUES (%s, %s, %s, now(), %s, 'quota_exceeded', %s)
        """,
        (email, code_hash, purpose, ip_address, detail[:500]),
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
_GENERIC_LOGIN_ERROR = "Invalid email or password."


def _consume_active_otp(conn: DictConnection, *, email: str, code: str) -> None:
    """Check a submitted code against the single still-live OTP for that
    address (idx_otp_codes_email_active's exact query) and consume it on
    success. Raises the SAME ValidationError with the SAME message on
    every failure path (no active code, expired, already consumed, wrong
    code, domain no longer allowed) -- deliberately generic so a caller
    cannot distinguish one failure reason from another, which would
    otherwise leak whether a request was ever made for that address.
    """
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
    # No explicit commit here: the caller continues in the SAME
    # transaction (setting the password, etc.) -- consuming the code and
    # whatever it unlocks commit together, atomically, or not at all.


def set_password(conn: DictConnection, *, email: str, code: str, new_password: str) -> AuthenticatedUser:
    """Signup (first-ever password) AND password reset (overwrite an
    existing one) -- the SAME call, per the owner-ruled model (module
    docstring). Consumes the OTP (proof of mailbox ownership) and
    get-or-creates the `users` row with the new Argon2id hash. This is
    the ONLY place `users.password_hash` is ever written.

    Password-strength validation runs BEFORE the OTP is consumed, so a
    rejected-for-weakness password doesn't burn the caller's code --
    they can retry with a stronger password using the same code.
    """
    email = normalize_email(email)
    _validate_password_strength(new_password, email=email)
    _consume_active_otp(conn, email=email, code=code)

    user_row = _get_or_create_user(conn, email)
    password_hash = hash_password(new_password)
    conn.execute(
        "UPDATE users SET password_hash = %s, last_login_at = now() WHERE id = %s",
        (password_hash, user_row["id"]),
    )
    logger.info("password_set", extra={"email_masked": mask(email)})
    # No explicit commit: same atomicity reasoning as _consume_active_otp()
    # above -- the caller's `with get_connection()` commits this together
    # with create_session() on the success path.

    return AuthenticatedUser(
        id=user_row["id"],
        email=user_row["email"],
        role=user_row["role"],
        display_name=user_row["display_name"],
    )


def login(conn: DictConnection, *, email: str, password: str, ip_address: str) -> AuthenticatedUser:
    """Password sign-in -- the primary auth path under the new model.
    Rate-limited per email AND per IP (login_attempts,
    migrations/0010_password_auth.sql) exactly like OTP requests.

    No enumeration: non-allowlisted domain, nonexistent account, an
    account that exists but has never set a password (password_hash ==
    ''), and a genuinely wrong password all raise the identical
    ValidationError with the identical message. verify_password() always
    does one real Argon2 verify (against a fixed dummy hash when there is
    no real one to check), so this holds for timing too, not just the
    response body.
    """
    email = normalize_email(email)

    _enforce_login_rate_limits(conn, email=email, ip_address=ip_address)
    _record_login_attempt(conn, email=email, ip_address=ip_address)  # counts regardless of outcome; commits itself

    if not is_domain_allowed(conn, email):
        verify_password(password, "")  # keep this branch's cost comparable to the real-lookup branch below
        raise ValidationError(_GENERIC_LOGIN_ERROR)

    row = conn.execute(
        "SELECT id, email, role, display_name, password_hash FROM users WHERE email = %s", (email,)
    ).fetchone()
    stored_hash = row["password_hash"] if row is not None else ""

    if not verify_password(password, stored_hash):
        raise ValidationError(_GENERIC_LOGIN_ERROR)

    assert row is not None  # verify_password() only returns True when stored_hash was non-empty, which requires `row`
    conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s", (row["id"],))

    return AuthenticatedUser(id=row["id"], email=row["email"], role=row["role"], display_name=row["display_name"])


# ── Change password (AUTH-V2 addendum: an authenticated user rotating
# their own password) ─────────────────────────────────────────────────


_GENERIC_CHANGE_PASSWORD_ERROR = "Current password is incorrect."


def _enforce_change_password_rate_limits(conn: DictConnection, *, user_id: int, ip_address: str) -> None:
    """Same shape as _enforce_login_rate_limits(), over
    password_change_attempts (migrations/0011_change_password_attempts.sql)
    keyed by user_id instead of email -- see CHANGE_PASSWORD_RATE_LIMIT_*'s
    comment above for why no plus-addressing canonicalization is needed
    here (the caller is already resolved to one specific account by their
    session, not an attacker-supplied address).
    """
    user_row = conn.execute(
        "SELECT count(*) AS n FROM password_change_attempts WHERE user_id = %s "
        "AND created_at > now() - (%s || ' minutes')::interval",
        (user_id, CHANGE_PASSWORD_RATE_LIMIT_WINDOW_MINUTES),
    ).fetchone()
    if user_row is not None and user_row["n"] >= CHANGE_PASSWORD_RATE_LIMIT_PER_USER:
        raise PasswordChangeRateLimited("Too many password change attempts. Try again later.")

    if ip_address:
        ip_row = conn.execute(
            "SELECT count(*) AS n FROM password_change_attempts WHERE ip_address = %s "
            "AND created_at > now() - (%s || ' minutes')::interval",
            (ip_address, CHANGE_PASSWORD_RATE_LIMIT_WINDOW_MINUTES),
        ).fetchone()
        if ip_row is not None and ip_row["n"] >= CHANGE_PASSWORD_RATE_LIMIT_PER_IP:
            raise PasswordChangeRateLimited("Too many password change attempts from this network. Try again later.")


def _record_change_password_attempt(conn: DictConnection, *, user_id: int, ip_address: str) -> None:
    # Same reasoning as _record_login_attempt(): recorded regardless of
    # outcome, before the current-password check runs, and committed
    # immediately so it survives even if something later raises.
    conn.execute(
        "INSERT INTO password_change_attempts (user_id, ip_address) VALUES (%s, %s)", (user_id, ip_address)
    )
    conn.commit()


def change_password(
    conn: DictConnection,
    *,
    user_id: int,
    email: str,
    current_password: str,
    new_password: str,
    ip_address: str,
    keep_session_id: int | None,
) -> None:
    """Rotate an already-authenticated user's own password. Requires the
    CURRENT password even though the caller already holds a valid
    session -- a stolen session token alone must not be enough to
    permanently take over the account (e.g. silently locking the real
    owner out by changing their password without knowing it). Verified
    with the same constant-time verify_password() login() uses.

    Rate-limited per user AND per IP (password_change_attempts,
    migrations/0011_change_password_attempts.sql): asking for the
    current password necessarily means a wrong guess and a right guess
    get distinguishably different responses (that is the whole point of
    the check) -- which makes this endpoint a guessing oracle against
    the real password unless it is rate-limited exactly like login().

    An account with password_hash == '' (no password set yet -- a
    brand-new row, or the seeded bootstrap admin before its first
    OTP->set-password flow) cannot use this path AT ALL -- checked
    explicitly against the stored hash rather than left to fall out of
    verify_password()'s empty-hash handling, which is deliberately
    permissive about what "current_password" value it's compared
    against (the dummy-hash timing shim) and must not be mistaken for
    "any current_password succeeds when there's no real password set."
    Such an account must go through request_otp()/set_password() instead.

    On success, every OTHER session for this user is revoked --
    `keep_session_id` (the caller's own, just-used session) is
    deliberately excluded so changing your password does not
    immediately sign yourself out -- but any OTHER session (e.g. one
    held by an attacker who stole the token before this rotation) stops
    working right away. This is the concrete "actually evicts an
    attacker" behavior a password change is supposed to have.

    Raises:
        PasswordChangeRateLimited -- per-user or per-IP limit hit.
        ValidationError -- no password set yet on this account, current
            password wrong, new password equals the current one, or the
            new password fails the same strength rules set_password()
            enforces.
    """
    _enforce_change_password_rate_limits(conn, user_id=user_id, ip_address=ip_address)
    _record_change_password_attempt(conn, user_id=user_id, ip_address=ip_address)

    row = conn.execute("SELECT password_hash FROM users WHERE id = %s", (user_id,)).fetchone()
    stored_hash = row["password_hash"] if row is not None else ""

    # Explicit, not an accidental empty-string match: this is the exact
    # edge a "verify current password" check can be bypassed at if
    # `current_password == ""` were simply compared against a
    # `password_hash` that is also "" -- checked and rejected BEFORE any
    # password comparison happens, regardless of what current_password
    # the caller supplied.
    if not stored_hash:
        raise ValidationError("No password is set on this account yet. Use the sign-up / reset flow to set one.")

    if not verify_password(current_password, stored_hash):
        raise ValidationError(_GENERIC_CHANGE_PASSWORD_ERROR)

    if new_password == current_password:
        raise ValidationError("New password must be different from your current password.")

    _validate_password_strength(new_password, email=email)

    new_hash = hash_password(new_password)
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s", (new_hash, user_id))

    if keep_session_id is not None:
        conn.execute(
            "UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL AND id <> %s",
            (user_id, keep_session_id),
        )
    else:
        conn.execute("UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL", (user_id,))

    logger.info("password_changed", extra={"email_masked": mask(email)})
    # No explicit commit: same atomicity reasoning as set_password() --
    # the caller's `with get_connection()` commits the password update
    # and the session revocations together, or not at all.


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

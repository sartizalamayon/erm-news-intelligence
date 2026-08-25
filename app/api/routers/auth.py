"""Auth endpoints -- OTP request, set-password (signup + password
reset), login, change-password, logout, and an admin-only view of
email-send failures.

Owner-ruled model change (supersedes ADR §9.3's "OTP only"; see
app.services.auth's module docstring for the full reasoning): OTP is
sent once per account, at signup and at password reset, to prove
mailbox ownership; every subsequent sign-in is email + password. The
design mock's password field (ADR §5) is now closer to the real flow
than the OTP-only ruling was, EXCEPT the mock's user-selectable role
picker -- still absent here on purpose (ADR §5 flags it as a security
defect, not a requirement; role always comes from the authenticated
account).

Three endpoints replace the frontend-contract.md §9 proposal's single
`POST /auth/login`:
  * `POST /auth/otp/request`   -- unchanged path; now takes an optional
    `purpose` ("signup" | "password_reset") that only changes the email
    copy. Same response/body shape and enumeration properties either way.
  * `POST /auth/password/set`  -- replaces the old `POST /auth/otp/verify`.
    Consumes the OTP and sets `users.password_hash` in one call (proves
    ownership and sets the password atomically -- see
    app.services.auth.set_password's docstring), then signs the caller
    in immediately, exactly like the old verify-OTP endpoint did.
  * `POST /auth/login`         -- NEW. Email + password, no OTP. This is
    the endpoint frontend-contract.md §3.0's `pw` field was always
    reaching for, now with a real server-side implementation instead of
    the mock's client-only checks.
`POST /auth/logout` is unchanged from the original proposal.

AUTH-V2 addendum: `POST /auth/password/change` -- an already-
authenticated user rotating their own password (no OTP/email involved).
Still requires the CURRENT password and is rate-limited per user AND
per IP -- see app.services.auth.change_password's docstring for why a
valid session alone is deliberately not sufficient.

See app.services.auth's module docstring for the full reasoning behind
every security-relevant choice made here (session mechanism, OTP
parameters, rate limits, enumeration handling, password rules, email
provider quota handling).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.api.schemas.auth import (
    ChangePasswordBody,
    LoginBody,
    LoginResponse,
    OtpFailureRow,
    OtpRequestBody,
    OtpRequestResponse,
    SetPasswordBody,
    SetPasswordResponse,
)
from app.config import Settings
from app.db.connection import get_connection
from app.logging import get_logger, mask
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)

# DoD #3: identical response body whether or not the address exists --
# this is the ONLY success body /otp/request ever returns, regardless of
# what request_otp() did internally (generated+sent a code, or found the
# email genuinely doesn't exist as an account -- which this endpoint
# never even checks; see app.services.auth's module docstring).
_OTP_SENT_MESSAGE = "If that address is eligible, a verification code has been sent."


def _trusted_proxy_hops() -> int:
    """Number of reverse-proxy hops, between the real client and this
    process, that are trusted to have each appended their own observed
    peer address onto X-Forwarded-For (RFC 7239's "rightmost trusted
    hop" model: each hop appends exactly one entry as it forwards, so
    only entries a KNOWN hop actually added can be relied on).

    Read from `app.config.Settings.trusted_proxy_hops` (ARCH-REPO added
    this as a proper typed setting once AUTH-OTP's original blocker --
    not owning `app/config.py`, the identical constraint
    app/services/email.py's docstring describes for the Resend API key
    -- was resolved; this previously read `os.environ["TRUSTED_PROXY_HOPS"]`
    directly). Deliberately constructs `Settings(_env_file=None)` fresh
    on every call rather than the process-wide cached `get_settings()`:
    this value genuinely needs to be operator-set per deployment
    topology -- Render alone is 1 hop; an additional CDN/WAF in front
    would be 2+ -- without a code change, so a fresh read (env vars
    only, no `.env` file, matching the pre-existing `os.environ.get`
    behavior exactly) is correct here, not a workaround.

    Defaults to 0: "trust nothing", per RT-5's remediation ("default to
    the safest interpretation... prefer the direct socket peer over a
    spoofable header"). At 0, X-Forwarded-For is never consulted at
    all, regardless of what it contains -- the previous behavior
    (trusting the LEFTMOST entry unconditionally) is exactly what let
    RT-5 spoof a fresh per-IP-rate-limit-evading value on every
    request, since the leftmost entry is always the position a client
    fully controls when a proxy appends rather than replaces.
    """
    # _env_file=None is a real, working pydantic-settings BaseSettings
    # kwarg at runtime (disables the .env-file source for this one
    # instance, env-vars-only) -- mypy just doesn't see it without the
    # pydantic mypy plugin, which isn't enabled here (pyproject.toml:
    # incompatible with the pinned mypy version in this environment).
    return Settings(_env_file=None).trusted_proxy_hops  # type: ignore[call-arg]


def _client_ip(request: Request) -> str:
    """Resolves the address used for per-IP OTP rate limiting (DoD #4;
    RT-5, docs/red-team-findings.md).

    With `_trusted_proxy_hops()` hops configured (> 0), the trustworthy
    value is the entry `hops` positions from the RIGHT of
    X-Forwarded-For -- the one appended by the trusted hop closest to
    the real client, never the leftmost entry (which is exactly the
    position an attacker fully controls whenever a proxy appends its
    own hop instead of replacing the header, which is standard
    behavior for nginx/Render/most L7 proxies). With the default of 0
    trusted hops, X-Forwarded-For is not consulted at all -- the direct
    socket peer (`request.client.host`) is always used, which is
    always correct for a single-hop deployment and fails safe for
    anything more complex (under-attributing distinct clients behind
    an unconfigured proxy to one IP is a usability cost, not a security
    hole; over-trusting a spoofable header, RT-5's actual bug, is the
    hole).
    """
    hops = _trusted_proxy_hops()
    if hops > 0:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            entries = [entry.strip() for entry in forwarded.split(",") if entry.strip()]
            if len(entries) >= hops:
                return entries[-hops]
    return request.client.host if request.client else ""


@router.post("/otp/request", status_code=status.HTTP_202_ACCEPTED, response_model=OtpRequestResponse)
def request_otp(body: OtpRequestBody, request: Request) -> OtpRequestResponse:
    ip_address = _client_ip(request)
    try:
        with get_connection() as conn:
            auth_service.request_otp(conn, email=body.email, ip_address=ip_address, purpose=body.purpose)
    except auth_service.OtpRateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    # app.errors.ValidationError -> 422 and app.errors.ProviderError ->
    # 503/502 are handled by the handlers app.main already registers
    # (app.api.error_handlers) -- nothing router-specific needed for
    # either. A ProviderError here means the email provider's quota is
    # exhausted or the send genuinely failed (DoD #5): the caller gets an
    # honest "unavailable" response rather than the success message
    # below, on purpose -- claiming success while quietly not sending is
    # exactly the silent-non-delivery DoD #5 rules out.
    return OtpRequestResponse(message=_OTP_SENT_MESSAGE)


@router.post("/password/set", response_model=SetPasswordResponse)
def set_password(body: SetPasswordBody, request: Request, response: Response) -> SetPasswordResponse:
    """Signup (first-ever password) AND password reset (overwrite an
    existing one) -- the same call (app.services.auth.set_password's
    docstring). Consumes the OTP and writes `users.password_hash` in one
    transaction, then signs the caller in immediately (same session
    mechanism as login()), so the client goes straight from "I just
    proved I own this mailbox and picked a password" to an authenticated
    session without a second round trip.
    """
    with get_connection() as conn:
        user = auth_service.set_password(conn, email=body.email, code=body.code, new_password=body.new_password)
        token = auth_service.create_session(
            conn,
            user_id=user.id,
            user_agent=request.headers.get("user-agent", "")[:500],
            ip_address=_client_ip(request),
        )
    response.set_cookie(value=token, **auth_service.cookie_kwargs())  # type: ignore[arg-type]
    logger.info("password_set_session_created", extra={"email_masked": mask(user.email), "role": user.role})
    return SetPasswordResponse(email=user.email, role=user.role, display_name=user.display_name)


@router.post("/login", response_model=LoginResponse)
def login(body: LoginBody, request: Request, response: Response) -> LoginResponse:
    """Password sign-in -- the primary auth path under the new model.
    Rate-limited per email AND per IP (auth_service.login /
    login_attempts) -- brute force is a new attack surface password auth
    introduces that OTP-only never had. Same no-enumeration contract as
    set_password()/request_otp(): a nonexistent account, an account that
    exists but never set a password, and a genuinely wrong password all
    come back as the identical 422.
    """
    ip_address = _client_ip(request)
    try:
        with get_connection() as conn:
            user = auth_service.login(conn, email=body.email, password=body.password, ip_address=ip_address)
            token = auth_service.create_session(
                conn,
                user_id=user.id,
                user_agent=request.headers.get("user-agent", "")[:500],
                ip_address=ip_address,
            )
    except auth_service.LoginRateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    response.set_cookie(value=token, **auth_service.cookie_kwargs())  # type: ignore[arg-type]
    logger.info("session_created", extra={"email_masked": mask(user.email), "role": user.role})
    return LoginResponse(email=user.email, role=user.role, display_name=user.display_name)


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordBody, request: Request, user: AuthenticatedUser = Depends(get_current_user)
) -> None:
    """AUTH-V2 addendum: an already-authenticated user rotating their
    own password. No OTP/email involved -- the caller's session cookie
    (via `get_current_user`) is what identifies the account, but
    `current_password` is still required (see
    app.services.auth.change_password's docstring for why a valid
    session alone must not be enough). Rate-limited per user AND per IP
    -- otherwise this is a password-guessing oracle, since a wrong
    current password necessarily gets a different response from a
    correct one. On success, every OTHER session for this user is
    revoked; the caller's own (just-used) session is deliberately kept
    alive.
    """
    ip_address = _client_ip(request)
    try:
        with get_connection() as conn:
            auth_service.change_password(
                conn,
                user_id=user.id,
                email=user.email,
                current_password=body.current_password,
                new_password=body.new_password,
                ip_address=ip_address,
                keep_session_id=user.session_id,
            )
    except auth_service.PasswordChangeRateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    logger.info("password_changed_via_api", extra={"email_masked": mask(user.email)})


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response) -> None:
    """Revokes the session server-side AND clears the browser's copy of
    the cookie.

    The deletion attributes come from `auth_service.cookie_deletion_kwargs()`
    -- i.e. from the same function that produced the attributes the
    cookie was SET with -- rather than being written out again here.
    This line used to be `delete_cookie(name, path="/")`, which defaults
    to `SameSite=Lax` and no `Secure`; in production the cookie is set
    `SameSite=None; Secure`, the frontend and API are genuinely
    cross-site (`onrender.com` is a Public Suffix), and a browser
    rejects a cross-site `Set-Cookie` that lacks `SameSite=None;
    Secure`. The clear was therefore dropped on the floor in exactly the
    environment it mattered in and nowhere else, so no local test of
    logout could ever have shown it. See
    `cookie_deletion_kwargs()`'s docstring for the full write-up,
    including why this was a UI-state bug and not an auth bypass.
    """
    token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    if token:
        with get_connection() as conn:
            auth_service.revoke_session(conn, token)
    response.delete_cookie(**auth_service.cookie_deletion_kwargs())  # type: ignore[arg-type]


@router.get("/otp-failures", response_model=list[OtpFailureRow])
def list_otp_failures(admin: AuthenticatedUser = Depends(require_admin)) -> list[OtpFailureRow]:
    """Admin-only surface for DoD #5's "quota exhaustion must fail
    VISIBLY... surfaced to admins" -- backed by
    migrations/0008_otp_send_status.sql. `require_admin` (app.api.deps)
    is the reusable dependency DoD #7 asks for; this route is also this
    package's own proof that it works end to end.
    """
    with get_connection() as conn:
        rows = auth_service.recent_send_failures(conn)
    return [
        OtpFailureRow(
            email_masked=mask(str(row["email"])),
            send_status=str(row["send_status"]),
            send_error=str(row["send_error"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]

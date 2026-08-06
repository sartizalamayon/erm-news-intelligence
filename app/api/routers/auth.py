"""Auth endpoints -- OTP request, OTP verify, logout, and an admin-only
view of email-send failures.

Implements ADR §9.3 (OTP-only auth, custom transport mandatory) and
frontend-contract.md §3.0 / §2, per the AUTH-OTP mission brief. The
design mock ships a password field with no OTP anywhere (ADR §5) --
this is the *ruled* OTP flow, not the mock's password flow, and there is
no user-selectable role anywhere below (ADR §5 flags that as a security
defect in the mock, not a requirement).

Two endpoints instead of the frontend-contract.md §9 proposal's single
`POST /auth/login`: that proposal predates the OTP ruling (§9 is
explicitly "a design proposal... kept separate from the observed-fact
sections", written against the mock's single-step password submit).
OTP is inherently two round trips -- request a code, then verify it --
so `POST /auth/otp/request` and `POST /auth/otp/verify` replace it.
`POST /auth/logout` is unchanged from that proposal.

See app.services.auth's module docstring for the full reasoning behind
every security-relevant choice made here (session mechanism, OTP
parameters, rate limits, enumeration handling, Resend quota handling).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.auth import (
    OtpFailureRow,
    OtpRequestBody,
    OtpRequestResponse,
    OtpVerifyBody,
    OtpVerifyResponse,
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
            auth_service.request_otp(conn, email=body.email, ip_address=ip_address)
    except auth_service.OtpRateLimited as exc:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
    # app.errors.ValidationError -> 422 and app.errors.ProviderError ->
    # 503/502 are handled by the handlers app.main already registers
    # (app.api.error_handlers) -- nothing router-specific needed for
    # either. A ProviderError here means Resend's quota is exhausted or
    # the send genuinely failed (DoD #5): the caller gets an honest
    # "unavailable" response rather than the success message below, on
    # purpose -- claiming success while quietly not sending is exactly
    # the silent-non-delivery DoD #5 rules out.
    return OtpRequestResponse(message=_OTP_SENT_MESSAGE)


@router.post("/otp/verify", response_model=OtpVerifyResponse)
def verify_otp(body: OtpVerifyBody, request: Request, response: Response) -> OtpVerifyResponse:
    with get_connection() as conn:
        user = auth_service.verify_otp(conn, email=body.email, code=body.code)
        token = auth_service.create_session(
            conn,
            user_id=user.id,
            user_agent=request.headers.get("user-agent", "")[:500],
            ip_address=_client_ip(request),
        )
    response.set_cookie(value=token, **auth_service.cookie_kwargs())  # type: ignore[arg-type]
    logger.info("session_created", extra={"email_masked": mask(user.email), "role": user.role})
    return OtpVerifyResponse(email=user.email, role=user.role, display_name=user.display_name)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    if token:
        with get_connection() as conn:
            auth_service.revoke_session(conn, token)
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path="/")


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

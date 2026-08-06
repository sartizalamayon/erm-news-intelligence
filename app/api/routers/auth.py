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


def _client_ip(request: Request) -> str:
    # Render (ADR §1's API host) terminates TLS and sets X-Forwarded-For
    # itself for traffic that reaches this process at all, so the first
    # hop is trustworthy in that deployment; falls back to the direct
    # peer address (e.g. the in-process TestClient used by this
    # package's own tests, where request.client is set but there is no
    # proxy in front of it at all).
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
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

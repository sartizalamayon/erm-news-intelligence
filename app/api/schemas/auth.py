"""Request/response models for the OTP auth flow (app.api.routers.auth,
app.api.routers.me).

Plain `str` fields with a hand-rolled regex, not pydantic's `EmailStr`:
`EmailStr` requires the optional `email-validator` package, which is not
installed in this environment and this task must not add new
dependencies (pyproject.toml is off-limits, and disk is at 99%). The
regex here only needs to reject obviously-malformed input before it
reaches app.services.auth -- the real authority on whether an address is
allowed is the server-side domain check there (DoD #1), not this schema.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OTP_CODE_RE = re.compile(r"^\d{6}$")


def _validate_email_shape(value: str) -> str:
    value = value.strip()
    if not value or not _EMAIL_RE.match(value):
        raise ValueError("Enter a valid email address.")
    return value


class OtpRequestBody(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email_shape(value)


class OtpRequestResponse(BaseModel):
    message: str


class OtpVerifyBody(BaseModel):
    email: str
    code: str

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email_shape(value)

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        value = value.strip()
        if not _OTP_CODE_RE.match(value):
            raise ValueError("Enter the 6-digit code.")
        return value


class OtpVerifyResponse(BaseModel):
    email: str
    role: str
    display_name: str


class MeResponse(BaseModel):
    """Backs GET /me (frontend-contract.md §2's shell contract:
    userEmail / initials / isAdmin / userName, all "Server -- derived
    from the authenticated user's role").
    """

    email: str
    display_name: str
    initials: str
    role: str
    is_admin: bool


class OtpFailureRow(BaseModel):
    """One row of the admin-only GET /auth/otp-failures view (DoD #5:
    quota exhaustion / send failures must be surfaced to admins).
    """

    email_masked: str
    send_status: str
    send_error: str
    created_at: str

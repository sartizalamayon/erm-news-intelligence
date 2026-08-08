"""Request/response models for the auth flow (app.api.routers.auth,
app.api.routers.me).

Owner-ruled model (app.services.auth's module docstring): OTP once per
account (signup, and password reset) to prove mailbox ownership; every
subsequent sign-in is email + password. Four request bodies below map
1:1 to the four things a client can do: request a code
(`OtpRequestBody`), consume it together with a new password
(`SetPasswordBody` -- covers both signup and password reset, same
mechanism), sign in with a password (`LoginBody`), and the read-only
admin/session views (`OtpFailureRow`, `MeResponse`) unchanged from the
OTP-only design.

Plain `str` fields with a hand-rolled regex, not pydantic's `EmailStr`:
`EmailStr` requires the optional `email-validator` package, which is not
installed in this environment and this task must not add new
dependencies (pyproject.toml is off-limits). The regex here only needs
to reject obviously-malformed input before it reaches app.services.auth
-- the real authority on whether an address is allowed is the
server-side domain check there (DoD #1), not this schema. Password
fields get a generous `max_length` (defense in depth against an
absurdly long input driving up Argon2's hashing cost -- app.config's
own `max_request_body_bytes` already bounds the whole request, this is
a tighter, field-specific bound) but no `min_length` here: the real
strength check (DoD #4: length + weak-value + email-contains-password)
lives server-side in app.services.auth._validate_password_strength so
its rules stay in one place, not duplicated/drifting between schema and
service.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_OTP_CODE_RE = re.compile(r"^\d{6}$")
_PASSWORD_MAX_LENGTH = 256


def _validate_email_shape(value: str) -> str:
    value = value.strip()
    if not value or not _EMAIL_RE.match(value):
        raise ValueError("Enter a valid email address.")
    return value


def _validate_code_shape(value: str) -> str:
    value = value.strip()
    if not _OTP_CODE_RE.match(value):
        raise ValueError("Enter the 6-digit code.")
    return value


class OtpRequestBody(BaseModel):
    """Backs POST /auth/otp/request -- used for BOTH signup (first-ever
    password) and password reset; `purpose` only changes the email copy
    (app.services.email), never a security property.
    """

    email: str
    purpose: Literal["signup", "password_reset"] = "signup"

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email_shape(value)


class OtpRequestResponse(BaseModel):
    message: str


class SetPasswordBody(BaseModel):
    """Backs POST /auth/password/set -- consumes the OTP and sets
    `users.password_hash` in one call (proves mailbox ownership and sets
    the password atomically; see app.services.auth.set_password's
    docstring). The SAME endpoint for signup's first-ever password and a
    password reset's overwrite -- both are "prove you own this mailbox,
    then set a password," which is exactly what one OTP + one new
    password already establishes.
    """

    email: str
    code: str
    new_password: str = Field(max_length=_PASSWORD_MAX_LENGTH)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email_shape(value)

    @field_validator("code")
    @classmethod
    def _check_code(cls, value: str) -> str:
        return _validate_code_shape(value)


class SetPasswordResponse(BaseModel):
    """Same shape as LoginResponse -- setting a password (signup or
    reset) signs the caller in immediately, exactly like login() does.
    """

    email: str
    role: str
    display_name: str


class LoginBody(BaseModel):
    """Backs POST /auth/login -- the primary sign-in path under the new
    model. No OTP involved; app.services.auth.login() does the rate
    limiting, constant-time verify, and no-enumeration handling.
    """

    email: str
    password: str = Field(max_length=_PASSWORD_MAX_LENGTH)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        return _validate_email_shape(value)


class LoginResponse(BaseModel):
    email: str
    role: str
    display_name: str


class ChangePasswordBody(BaseModel):
    """Backs POST /auth/password/change (AUTH-V2 addendum) -- an
    already-authenticated user (resolved from their session cookie, not
    from a body field) rotating their own password. No email/OTP
    involved; `current_password` is required precisely BECAUSE the
    caller already has a valid session -- see
    app.services.auth.change_password's docstring.
    """

    current_password: str = Field(max_length=_PASSWORD_MAX_LENGTH)
    new_password: str = Field(max_length=_PASSWORD_MAX_LENGTH)


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

"""Auth endpoints — login (OTP request + verify), logout.

Stub only. TODO(AUTH-OTP): implement per ADR §9.3 (OTP-only auth, custom
SMTP mandatory — see app.config's smtp_* settings) and
frontend-contract.md §3.0 / §9 ("POST /auth/login", "POST /auth/logout").
Note the design mock ships a password field with no OTP anywhere
(ADR §5) — AUTH-OTP is building the *ruled* OTP flow, not the mock's
password flow.

Do not add routes here until AUTH-OTP starts; this file exists only so
the router is wired into app/main.py and the path is reserved.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])

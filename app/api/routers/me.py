"""Authenticated-shell endpoints shared by every screen: current user,
current run status. See frontend-contract.md §2 (global shell contract)
and §9 ("GET /me", "GET /runs/current", "POST /runs/current/cancel").

Stub only. TODO(AUTH-OTP for GET /me; P1-FETCH/ARCH-DB for run-status
plumbing behind GET /runs/current and POST /runs/current/cancel — a
real cancel-job mechanism doesn't exist in the legacy backend either,
per frontend-contract.md §2).
"""

from fastapi import APIRouter

router = APIRouter(tags=["shell"])

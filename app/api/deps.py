"""Shared FastAPI dependencies (`Depends(...)`).

Empty of real logic on purpose — no auth mechanism has been decided
beyond "OTP only" (ADR §9.3 rules OTP-only auth but not the session
mechanism: JWT vs. signed cookie vs. Supabase's own auth primitives are
all still open). AUTH-OTP owns that decision and this file.

TODO(AUTH-OTP): implement a real `get_current_user` dependency and wire
it into every router in app/api/routers/ that needs it (per
frontend-contract.md §8: admin screens must be enforced server-side, not
just hidden in the nav). If a new third-party package is needed for
session/token handling, flag it back to ARCH-REPO — this repo's
convention is that only the dependency-manifest owner edits
pyproject.toml.
"""

from __future__ import annotations


def get_current_user() -> None:
    """Placeholder. Raises so it's obvious if something starts depending
    on this before AUTH-OTP implements it for real.
    """
    raise NotImplementedError("AUTH-OTP: implement session/user resolution here")

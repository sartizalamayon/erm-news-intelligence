"""Logging out must clear the cookie in the browser that actually runs
this app, not just in the one a local test happens to use.

The defect: `POST /auth/logout` called
`response.delete_cookie(SESSION_COOKIE_NAME, path="/")`. Starlette's
`delete_cookie` defaults to `samesite="lax"` and `secure=False`, so in
production it emitted a `Set-Cookie` carrying `SameSite=Lax` and no
`Secure` -- while `auth_service.cookie_kwargs()` had SET the cookie with
`SameSite=None; Secure`, because ADR §1 puts the frontend on a Render
Static Site and the API on a Render web service.

Those are not two subdomains of one site. `onrender.com` is on the Public
Suffix List, so `erm-frontend.onrender.com` and `erm-backend.onrender.com`
are separate SITES, and every call between them is cross-site. Chrome
rejects a cross-site `Set-Cookie` that does not carry
`SameSite=None; Secure` -- so the deletion header was discarded by the
browser and the stale cookie stayed in the jar.

Not an auth bypass, and worth being precise about why: `revoke_session`
writes `sessions.revoked_at` before the response is built, so the token
is dead server-side the instant logout returns. The damage is to what the
user sees -- the browser keeps presenting a cookie that looks like a
session, so the shell can boot into a logged-in-looking layout until the
first API call returns 401. On a shared machine, "did that log me out?"
is not a question the UI is allowed to be ambiguous about.

Why it survived every existing test: the two attributes only differ in
production/staging. In `APP_ENV=test`, `cookie_kwargs()` returns
`samesite="lax"`, `secure=False` -- byte-identical to `delete_cookie`'s
own defaults. The bug was invisible in the only environment the suite
ran in. Every test below therefore asserts under `production_env`.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.services import auth as auth_service


@pytest.fixture
def production_client(production_env):
    """A TestClient built AFTER `production_env` has set the env and
    cleared the settings cache, so `cookie_kwargs()` resolves the
    production branch at request time. Not the shared `client` fixture:
    that one is deliberately environment-agnostic, and the whole point
    here is the environment.
    """
    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


def _set_cookie_header(response) -> str:
    header = response.headers.get("set-cookie")
    assert header is not None, "logout sent no Set-Cookie at all -- nothing can clear the cookie"
    return header


def test_logout_deletion_cookie_is_samesite_none_and_secure_in_production(production_client):
    """The regression itself. Without the fix this header reads
    `SameSite=lax` with no `Secure`, and the browser drops it.
    """
    response = production_client.post("/auth/logout")
    assert response.status_code == 204
    header = _set_cookie_header(response).lower()
    assert "samesite=none" in header
    assert "secure" in header


def test_logout_deletion_cookie_still_expires_the_cookie(production_client):
    """The attributes are only half of it -- the header still has to be
    a deletion. `delete_cookie` supplies `Max-Age=0`/`Expires` itself,
    which is exactly why `cookie_deletion_kwargs()` drops `max_age`
    rather than passing the live cookie's 12-hour TTL back in.
    """
    header = _set_cookie_header(production_client.post("/auth/logout")).lower()
    assert auth_service.SESSION_COOKIE_NAME.lower() in header
    assert "max-age=0" in header
    assert "path=/" in header


def test_logout_deletion_cookie_matches_the_cookie_that_was_set(production_client, production_env):
    """The invariant behind the fix, stated as an invariant.

    A `Set-Cookie` can only overwrite an existing cookie when name, Path
    and Domain match, and the browser applies its own acceptance rules to
    the deletion header on top of that. So every attribute the SET uses
    has to appear on the DELETE, with the sole exception of `max_age` --
    which `delete_cookie` replaces with its own zero. Asserted against
    `cookie_kwargs()` rather than against literals so that a future
    change to the set (a `domain`, say) fails here instead of silently
    producing an undeletable cookie again.
    """
    set_kwargs = auth_service.cookie_kwargs()
    delete_kwargs = auth_service.cookie_deletion_kwargs()

    assert set(set_kwargs) - set(delete_kwargs) == {"max_age"}
    for name, value in delete_kwargs.items():
        assert set_kwargs[name] == value, f"logout would send a different {name} than login set"


def test_non_production_logout_is_unchanged(client):
    """Local dev and CI are same-site over plain http, where `Secure`
    would make the browser drop the cookie entirely. The fix must not
    have quietly made dev behave like production -- it derives both from
    `app_env`, so this is the other half of that derivation.
    """
    header = _set_cookie_header(client.post("/auth/logout")).lower()
    assert "samesite=lax" in header
    assert "secure" not in header

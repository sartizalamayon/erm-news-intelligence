"""This API does not know it is behind TLS, so it must never be allowed
to say so out loud.

Render terminates TLS at its load balancer and forwards plain HTTP to
this process. uvicorn's `forwarded_allow_ips` defaults to `127.0.0.1`
and Render's proxy is not on loopback, so `X-Forwarded-Proto` is ignored
and `request.url.scheme` is `"http"` on every request in production.

Exactly one thing in this process builds an absolute URL out of that
scheme: Starlette's slash-redirect. Ask for `/health/ready/` and it
answers `307 Location: http://<api-host>/health/ready` -- a downgrade the
browser will not follow from an https page on a credentialed
cross-origin call. It blocks it as mixed content and the caller sees an
opaque network error rather than a redirect. Verified directly: with
`redirect_slashes` left on, `GET /healthz/` returns
`307 -> http://testserver/healthz`.

Nothing depends on that redirect today (every collection route is
declared `@router.get("")` and the frontend never appends a trailing
slash), which is why it could be removed rather than repaired -- and
why it would otherwise have sat there as a trap for the first route or
client that ever did depend on it.

WHY THE OBVIOUS ONE-FLAG FIX IS NOT USED, and why a test guards against
it: `uvicorn --forwarded-allow-ips="*"` would fix the scheme. It would
also, in the same middleware, overwrite `scope["client"]` with the
LEFTMOST `X-Forwarded-For` entry -- the one position a caller fully
controls, because proxies append rather than replace. Both
`app.api.routers.auth._client_ip` and
`app.middleware.rate_limit.resolve_client_ip` fall back to
`request.client.host`, and `TRUSTED_PROXY_HOPS=0` -- the fail-safe
default, and what any unparsable value resolves to -- makes that
fallback the only path. So the flag would turn "trust nothing" into
"trust the header verbatim" at precisely the setting meant to be
safest, reopening the per-IP OTP rate-limit bypass RT-5 found
(docs/red-team-findings.md). Re-enabling slash redirects requires
giving the app a correct notion of its own scheme first; it is not a
flag flip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.main import create_app

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Real, registered paths -- one on the app itself, one behind a router
# prefix, so both the app router and an included router are covered.
_REAL_PATHS = ("/healthz", "/health/ready")


@pytest.fixture
def no_redirect_client():
    with TestClient(create_app(), follow_redirects=False) as client:
        yield client


@pytest.mark.parametrize("path", _REAL_PATHS)
def test_the_path_under_test_is_actually_a_route(no_redirect_client, path: str):
    """Guards the guards below: they assert "no 307 for path + '/'",
    which a path that was never routed at all would satisfy for the
    wrong reason.
    """
    assert no_redirect_client.get(path).status_code == 200


@pytest.mark.parametrize("path", _REAL_PATHS)
def test_a_trailing_slash_never_produces_a_scheme_downgrading_redirect(no_redirect_client, path: str):
    """THE regression. With `redirect_slashes` on, this is a 307 whose
    `Location` starts `http://` -- which is the whole defect, since that
    header is generated from a scheme this process cannot observe
    correctly behind Render's TLS termination.
    """
    response = no_redirect_client.get(path + "/")
    assert response.status_code != 307
    location = response.headers.get("location")
    assert location is None or not location.startswith("http://"), (
        f"{path}/ produced a redirect to {location!r}. A browser on the https "
        "frontend blocks that as a mixed-content downgrade; the scheme is wrong "
        "because uvicorn is deliberately not trusting X-Forwarded-Proto here."
    )


def test_slash_redirects_are_off_at_the_router_level():
    """Stated once as the mechanism, so the parametrized tests above are
    not the only thing standing between this and a silent re-enable by
    something that rebuilds the app differently.
    """
    assert create_app().router.redirect_slashes is False


def test_render_start_command_does_not_trust_forwarded_headers():
    """The dangerous repair, blocked.

    `--forwarded-allow-ips` is a single switch for two behaviours, and
    the second one hands `request.client` to the caller (see this
    module's docstring). Someone will eventually notice the scheme is
    wrong and reach for it; this is the note they will find. If the
    scheme genuinely needs fixing, it needs a mechanism that sets the
    scheme WITHOUT touching client attribution -- not this flag.
    """
    blueprint = yaml.safe_load((_REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))
    start_commands = [s["startCommand"] for s in blueprint["services"] if s.get("startCommand")]
    assert start_commands, "render.yaml declares no startCommand -- this test would be vacuous"
    for command in start_commands:
        assert "forwarded-allow-ips" not in command, (
            "render.yaml's startCommand enables uvicorn's proxy-header trust. "
            "That also rewrites request.client from the leftmost X-Forwarded-For "
            "entry, which reopens RT-5's per-IP OTP rate-limit bypass whenever "
            "TRUSTED_PROXY_HOPS is at its fail-safe 0."
        )

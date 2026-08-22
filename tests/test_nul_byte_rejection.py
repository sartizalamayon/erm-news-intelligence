"""A NUL byte must not turn a request into an opaque 500.

FOUND BY FUZZING THE LIVE API. Postgres cannot store NUL (0x00) in a
text column at all: psycopg raises
`DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes`.
Nothing caught it, so ONE stray byte turned every free-text endpoint
into `{"error":"internal_error"}`. Measured before the fix:

    GET  /search?q=            500
    GET  /articles?q=          500
    POST /keywords             500
    POST /sources              500
    PUT  /prompts/guidance     500

Rejected at the middleware boundary rather than per-route, so a route
added later inherits the guard.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.main import create_app

ADMIN = AuthenticatedUser(id=1, email="admin@example.com", role="admin", display_name="Admin", session_id=1)


def _client() -> TestClient:
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    app.dependency_overrides[require_admin] = lambda: ADMIN
    return TestClient(app)


def test_nul_in_a_query_string_is_a_400_not_a_500() -> None:
    with _client() as client:
        resp = client.get("/search", params={"q": "a\x00b"})
    assert resp.status_code == 400
    assert "NUL" in resp.json()["detail"]


def test_percent_encoded_nul_in_a_query_string_is_rejected() -> None:
    """`%00` and a literal NUL arrive as the same thing after decoding,
    so the raw query string is checked for both forms.
    """
    with _client() as client:
        resp = client.get("/search?q=a%00b")
    assert resp.status_code == 400


def test_nul_in_a_json_body_is_rejected() -> None:
    """The case a raw-byte scan MISSES.

    A JSON encoder never puts a raw 0x00 in the payload: it writes the
    six-character escape `\\u0000`. So the body reaches the middleware
    with no NUL byte in it, and the NUL only materialises after parsing,
    on its way to the database. The first version of this guard checked
    only for a raw byte and let every JSON body straight through.
    """
    with _client() as client:
        resp = client.post(
            "/keywords",
            json={"section": "Others", "term_en": "a\x00b", "term_bn": "", "enabled": True},
        )
    assert resp.status_code == 400
    assert "NUL" in resp.json()["detail"]


def test_a_clean_json_body_is_rewound_and_reaches_its_route() -> None:
    """The guard CONSUMES the body to inspect it, then has to replay it.

    Get that wrong and every JSON route hangs waiting for a body that
    was already read. Asserted without a database: what matters is that
    the request gets PAST the middleware, so any outcome except the
    middleware's own NUL rejection proves the rewind worked.
    """
    with _client() as client:
        resp = client.post(
            "/keywords",
            json={"section": "Others", "term_en": "nul-guard-probe", "term_bn": "", "enabled": True},
        )
    assert not (resp.status_code == 400 and "NUL" in resp.text), "a clean body was wrongly rejected"


def test_a_binary_upload_is_not_treated_as_text() -> None:
    """.xlsx bodies are legitimately full of NUL bytes. The guard only
    inspects a body it knows is JSON, or keyword import would become
    impossible.
    """
    with _client() as client:
        resp = client.post(
            "/keywords/import",
            content=b"PK\x03\x04\x00\x00 not really a workbook",
            headers={"Content-Type": "application/octet-stream"},
        )
    # Rejected as a bad workbook by the route, NOT as a bad byte by the
    # middleware: the point is that it reached the route at all.
    assert resp.status_code != 400 or "NUL" not in resp.text

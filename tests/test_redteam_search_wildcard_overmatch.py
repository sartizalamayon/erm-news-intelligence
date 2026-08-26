"""The Review queue's `q=` filter treated the analyst's own typing as a
SQL LIKE pattern, so a search could silently return the WHOLE ARCHIVE.

FOUND BY FUZZING THE LIVE API (pre-deploy adversarial pass). The list
endpoint builds its free-text filter in app/api/routers/review.py's
`_build_where` as:

    a.title ILIKE %(q)s OR a.title_en ILIKE %(q)s OR ...
    params["q"] = f"%{q}%"

Wrapping the raw value in `%...%` is right; passing the raw value
through is not. `%` and `_` are LIKE metacharacters, and nothing
escaped them, so the analyst's text was reinterpreted as pattern
syntax on its way to Postgres. Measured against a real migrated schema
before the fix:

    GET /articles?q=%     -> 200, total = EVERY ROW IN THE ARCHIVE
    GET /articles?q=_     -> 200, total = EVERY ROW IN THE ARCHIVE
    GET /articles?q=%_%   -> 200, total = EVERY ROW IN THE ARCHIVE

That is the exact failure mode this project keeps getting bitten by:
not an error, not an empty list, but a confident 200 carrying an
answer that is wrong. The review queue's header reads "N of Total"
straight off that number, so the screen states a specific, wrong count
and the analyst has no signal that their filter did not apply.

The realistic trigger is not a hostile `%`. It is a percentage, which
is ordinary vocabulary in this corpus (tariffs, coverage, revenue
growth, inflation — in both languages). An analyst searching for the
"100% network coverage" story got the "100 crore taka" story too,
because `%100%%` means "contains 100, then anything". Measured:

    q="100%"  buggy pattern %100%%   -> 2 hits (1 of them wrong)
    q="100%"  fixed pattern %100\\%%  -> 1 hit  (the one asked for)

Silent over-matching is worse than an error here: the extra rows look
like genuine results, so a wrong row can be approved into a brief.

Note the ASYMMETRY this file also pins: `GET /search` was never
affected. It goes through `plainto_tsquery('simple', ...)`
(app/services/search.py), which parses text as a search query rather
than as a pattern, so `%` there is punctuation and matches nothing.
Only the ILIKE path needed escaping, and the search-path assertions
below exist so a future "let's make search consistent with articles"
refactor cannot quietly reintroduce the hole from the other side.

Backslash is escaped FIRST in `_like_escape` for the usual reason: doing
it after `%`/`_` would re-escape the backslashes just inserted, turning
a search for `a%b` into a search for a literal backslash.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.db.testing import migrated_schema, pg_available
from app.main import create_app

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

USER = AuthenticatedUser(id=1, email="analyst@grameenphone.com", role="analyst", display_name="Analyst", session_id=1)

_url_seq = itertools.count()

# Deliberately bilingual: the wildcard bug is encoding-independent, but
# this product is Bangla/English and a fix that only ever gets exercised
# against ASCII headlines is not evidence about the corpus it runs on.
CORPUS = [
    "Operator hits 100% network coverage",
    "Revenue up 100 crore taka this quarter",
    "BTRC sets 50% spectrum fee cut",
    "বাংলাদেশ ব্যাংক নীতি সুদহার বাড়াল",
    "মূল্যস্ফীতি ১০০ শতাংশ ছাড়িয়েছে",
]


def _insert_article(conn, title: str) -> int:
    row = conn.execute(
        "INSERT INTO articles (url, url_norm, source, title, day, sections, keywords, review_status, is_primary) "
        "VALUES (%s, '', 'Prothom Alo', %s, %s, '', '', 'pending', TRUE) RETURNING id",
        (f"https://x.test/article-{next(_url_seq)}", title, date.today()),
    ).fetchone()
    return row["id"]


@pytest.fixture
def wildcard_client(monkeypatch: pytest.MonkeyPatch):
    import app.api.routers.review as review_router_module
    import app.api.routers.search as search_router_module

    with migrated_schema() as conn:
        holder = lambda: contextlib.nullcontext(conn)  # noqa: E731
        monkeypatch.setattr(review_router_module, "get_connection", holder)
        monkeypatch.setattr(search_router_module, "get_connection", holder)
        for title in CORPUS:
            _insert_article(conn, title)
        conn.commit()
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: USER
        with TestClient(app) as client:
            yield client, conn


def _total(client, q: str) -> int:
    resp = client.get("/articles", params={"q": q})
    assert resp.status_code == 200, resp.text
    return resp.json()["total"]


# ── the archive-wide over-match ────────────────────────────────────────


@pytest.mark.parametrize(
    ("wildcard", "expected"),
    [
        # Two headlines in CORPUS contain a literal percent sign, so the
        # RIGHT answer for `%` is 2 — not 0, and emphatically not 5.
        # Asserting the literal-match count rather than "nothing" is what
        # makes this a test of correct behaviour instead of a test that
        # the feature was disabled.
        ("%", 2),
        ("_", 0),
        ("%_%", 0),
        ("%%", 0),
        ("___", 0),
    ],
)
def test_a_bare_like_wildcard_does_not_return_the_whole_archive(
    wildcard_client, wildcard: str, expected: int
) -> None:
    """The headline defect: a query made only of LIKE metacharacters
    matched every row and reported it as a search result.

    Each of these returned len(CORPUS) before the fix. The second
    assertion states the actual invariant in its own right, so the test
    keeps its meaning even if CORPUS grows later.
    """
    client, _ = wildcard_client
    total = _total(client, wildcard)
    assert total == expected
    assert total < len(CORPUS), "a metacharacter query matched the entire archive"


def test_percent_in_a_real_query_matches_only_the_literal_percent(wildcard_client) -> None:
    """The trigger an analyst actually hits. Percentages are everyday
    vocabulary in this corpus; before the fix `100%` also dragged in
    "100 crore taka" because the trailing `%` meant "then anything".
    """
    client, _ = wildcard_client
    resp = client.get("/articles", params={"q": "100%"})
    assert resp.status_code == 200
    titles = [i["title"] for i in resp.json()["items"]]
    assert titles == ["Operator hits 100% network coverage"]


def test_underscore_is_a_literal_not_a_single_character_wildcard(wildcard_client) -> None:
    """`_` matches exactly one character in LIKE. Unescaped, `q=BTRC_`
    would match "BTRC " (and so the spectrum headline); escaped, it
    looks for a literal underscore, which nothing in CORPUS has.
    """
    client, _ = wildcard_client
    assert _total(client, "BTRC_") == 0
    assert _total(client, "BTRC") == 1


def test_backslash_is_escaped_before_the_metacharacters(wildcard_client) -> None:
    """Order-of-escaping guard. If `_like_escape` replaced `%`/`_`
    before backslashes, the backslashes it had just inserted would be
    escaped a second time and the pattern would search for a literal
    backslash instead of the user's text. A plain search must keep
    working, and a lone backslash must be inert rather than a SQL error.
    """
    client, _ = wildcard_client
    assert _total(client, "\\") == 0
    assert _total(client, "\\%") == 0
    assert _total(client, "spectrum") == 1


def test_bangla_search_still_matches_after_escaping(wildcard_client) -> None:
    """Escaping must not disturb non-ASCII text — this corpus is
    majority Bangla, so a regression here would be far more damaging
    than the bug being fixed.
    """
    client, _ = wildcard_client
    assert _total(client, "ব্যাংক") == 1
    assert _total(client, "মূল্যস্ফীতি") == 1


# ── the search endpoint was never affected; keep it that way ──────────


@pytest.mark.parametrize("wildcard", ["%", "_", "%_%", "!!!"])
def test_search_endpoint_never_matched_everything(wildcard_client, wildcard: str) -> None:
    """`GET /search` goes through plainto_tsquery, not ILIKE, so
    metacharacters and pure punctuation parse to an EMPTY tsquery and
    match nothing. This asserts the property directly so it survives a
    future refactor toward the ILIKE style.
    """
    client, _ = wildcard_client
    resp = client.get("/search", params={"q": wildcard})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 0
    # corpusSize is the honest "how big is the archive" number and must
    # still be reported — it is not a match count.
    assert body["corpusSize"] == len(CORPUS)

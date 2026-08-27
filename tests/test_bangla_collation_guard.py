"""The database locale that silently deletes half the product.

Postgres `\\y` (word boundary) decides what counts as a word character
from LC_CTYPE. Roughly half this corpus is Bangla, and every pattern in
app/db/econaxes.py and app/db/watchlist.py uses `\\y`. Under plain `C`
or `POSIX` those patterns match NOTHING in Bangla, and nothing errors:
collection runs, the pipeline completes, the dashboard renders, and
every Bangla-derived figure is quietly near zero, which is
indistinguishable from a quiet news day.

This is not hypothetical. It is how the FIRST CI run this project ever
performed failed: the runner's Postgres container had a non-UTF-8
locale, and exactly two Bangla matching tests failed while 1589 others
passed. The machine the patterns were written on runs `en_US.UTF-8`, so
nothing local could have caught it, and a managed provider gives you
whatever locale it was provisioned with.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from app.db.collation_guard import verify_bangla_word_boundaries
from app.db.testing import _candidate_dsn, pg_available
from app.errors import ConfigurationError

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

BANKING_PATTERN = r"\yব্যাংক\y"
REAL_HEADLINE = "ডাচ-বাংলা ব্যাংকের দ্বিতীয় প্রান্তিকে মুনাফা বেড়েছে"
BANGKOK = "ব্যাংকক পেট এক্সপো শুরু"


def _db_with_locale(locale: str):
    """A throwaway database created with `locale`, or None if the host
    does not have it installed."""
    base = _candidate_dsn()
    name = f"loctest_{uuid.uuid4().hex[:8]}"
    admin = psycopg.connect(base, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{name}" TEMPLATE template0 LOCALE %s ENCODING \'UTF8\'' % f"'{locale}'")
    except psycopg.Error:
        admin.close()
        return None, None
    return base.rsplit("/", 1)[0] + "/" + name, (admin, name)


def _drop(handle) -> None:
    admin, name = handle
    admin.execute(f'DROP DATABASE "{name}"')
    admin.close()


@pytest.mark.parametrize("locale", ["C.UTF-8", "en_US.UTF-8"])
def test_a_utf8_locale_matches_bangla_and_still_refuses_bangkok(locale: str) -> None:
    dsn, handle = _db_with_locale(locale)
    if dsn is None:
        pytest.skip(f"{locale} is not installed on this host")
    try:
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
            assert conn.execute("SELECT %s ~ %s AS m", (REAL_HEADLINE, BANKING_PATTERN)).fetchone()["m"] is True
            # The boundary must still do its job in the other direction:
            # a locale that matched everything would pass a bare
            # positive check while being equally wrong.
            assert conn.execute("SELECT %s ~ %s AS m", (BANGKOK, BANKING_PATTERN)).fetchone()["m"] is False
            verify_bangla_word_boundaries(conn)  # must not raise
    finally:
        _drop(handle)


@pytest.mark.parametrize("locale", ["C", "POSIX"])
def test_a_non_utf8_locale_is_refused_rather_than_silently_matching_nothing(locale: str) -> None:
    dsn, handle = _db_with_locale(locale)
    if dsn is None:
        pytest.skip(f"{locale} is not installed on this host")
    try:
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
            # First establish the damage is real on this locale...
            assert conn.execute("SELECT %s ~ %s AS m", (REAL_HEADLINE, BANKING_PATTERN)).fetchone()["m"] is False
            # ...then that the guard converts it into a loud refusal.
            with pytest.raises(ConfigurationError) as exc:
                verify_bangla_word_boundaries(conn)
            message = str(exc.value)
            assert "Bangla" in message
            assert locale in message, "the message must name the offending locale, for the operator reading it"
            assert "C.UTF-8" in message, "and must name a locale that works"
    finally:
        _drop(handle)

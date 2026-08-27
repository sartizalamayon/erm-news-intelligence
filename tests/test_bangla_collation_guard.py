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


# Locale NAMES are deliberately not asserted here.
#
# The first version of this file parametrised ["C.UTF-8", "en_US.UTF-8"]
# as the working pair and ["C", "POSIX"] as the broken pair, measured on
# macOS. On the Linux runner, glibc's C.UTF-8 does NOT classify Bangla
# letters as alphabetic, so that "working" case was wrong on the only
# platform this deploys to -- a test encoding the same platform
# assumption the guard exists to catch.
#
# So these tests assert the guard AGREES WITH REALITY on whatever
# locales the host actually has: measure the real pattern first, then
# require the guard to accept exactly when matching works and refuse
# exactly when it does not. That holds on any libc.
CANDIDATE_LOCALES = ["C", "POSIX", "C.UTF-8", "en_US.UTF-8"]


@pytest.mark.parametrize("locale", CANDIDATE_LOCALES)
def test_the_guard_agrees_with_what_the_database_actually_does(locale: str) -> None:
    dsn, handle = _db_with_locale(locale)
    if dsn is None:
        pytest.skip(f"{locale} is not installed on this host")
    try:
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
            matches_real = conn.execute(
                "SELECT %s ~ %s AS m", (REAL_HEADLINE, BANKING_PATTERN)
            ).fetchone()["m"]
            matches_bangkok = conn.execute(
                "SELECT %s ~ %s AS m", (BANGKOK, BANKING_PATTERN)
            ).fetchone()["m"]
            usable = bool(matches_real) and not bool(matches_bangkok)

            if usable:
                # Must not raise: this locale can carry the product.
                verify_bangla_word_boundaries(conn)
            else:
                with pytest.raises(ConfigurationError) as exc:
                    verify_bangla_word_boundaries(conn)
                message = str(exc.value)
                assert "Bangla" in message
                assert locale in message, "the message must name the offending locale for the operator"
                assert "en_US.UTF-8" in message, "and must name one that works on the deployment platform"
    finally:
        _drop(handle)


def test_at_least_one_locale_on_this_host_is_usable() -> None:
    """Guards against the whole suite above passing vacuously.

    Every case is conditional on what the host has installed, so if no
    candidate locale existed they would all skip and prove nothing.
    """
    usable = []
    for locale in CANDIDATE_LOCALES:
        dsn, handle = _db_with_locale(locale)
        if dsn is None:
            continue
        try:
            with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
                if conn.execute("SELECT %s ~ %s AS m", (REAL_HEADLINE, BANKING_PATTERN)).fetchone()["m"]:
                    usable.append(locale)
        finally:
            _drop(handle)
    assert usable, (
        "no locale on this host can match Bangla word boundaries, so this suite "
        f"proved nothing. Tried: {CANDIDATE_LOCALES}"
    )

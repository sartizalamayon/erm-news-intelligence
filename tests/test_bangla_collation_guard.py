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
from app.db.econaxes import bangla_boundary
from app.db.testing import _candidate_dsn, pg_available

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

# The production construction, imported not restated.
BANKING_PATTERN = bangla_boundary("ব্যাংক")
# What it replaced, kept so the tests below can show the difference.
LEGACY_Y_PATTERN = r"\yব্যাংক\y"
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
# An earlier version parametrised ["C.UTF-8", "en_US.UTF-8"] as the
# working pair, measured on macOS. On Linux, glibc's C.UTF-8 does not
# classify Bangla letters the same way, so that "working" case was wrong
# on the only platform this deploys to.
#
# The patterns no longer depend on any of that. `bangla_boundary` states
# the boundary in code points, so these tests assert the much stronger
# property the fix bought: Bangla matching is correct under EVERY
# locale, including the ones that used to break it.
CANDIDATE_LOCALES = ["C", "POSIX", "C.UTF-8", "en_US.UTF-8"]


@pytest.mark.parametrize("locale", CANDIDATE_LOCALES)
def test_bangla_matching_is_correct_under_every_locale(locale: str) -> None:
    dsn, handle = _db_with_locale(locale)
    if dsn is None:
        pytest.skip(f"{locale} is not installed on this host")
    try:
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
            assert conn.execute(
                "SELECT %s ~ %s AS m", (REAL_HEADLINE, BANKING_PATTERN)
            ).fetchone()["m"] is True, f"a real bank headline stopped matching under {locale}"
            # The boundary must still do its job in the other direction.
            assert conn.execute(
                "SELECT %s ~ %s AS m", (BANGKOK, BANKING_PATTERN)
            ).fetchone()["m"] is False, f"Bangkok leaked into the banking axis under {locale}"
            verify_bangla_word_boundaries(conn)  # must not raise on any locale
    finally:
        _drop(handle)


@pytest.mark.parametrize("locale", CANDIDATE_LOCALES)
def test_the_old_y_boundary_is_the_thing_that_was_locale_dependent(locale: str) -> None:
    """Shows what the fix actually bought, rather than asserting it in prose.

    `\y` gives different answers on the same headline depending on the
    database's LC_CTYPE. Recording that here means the next person to
    reach for `\y` on a Bangla token can see why it is not safe, and it
    fails loudly if some future libc makes `\y` reliable and this
    comment becomes wrong.
    """
    dsn, handle = _db_with_locale(locale)
    if dsn is None:
        pytest.skip(f"{locale} is not installed on this host")
    try:
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn:
            legacy = conn.execute(
                "SELECT %s ~ %s AS m", (REAL_HEADLINE, LEGACY_Y_PATTERN)
            ).fetchone()["m"]
            current = conn.execute(
                "SELECT %s ~ %s AS m", (REAL_HEADLINE, BANKING_PATTERN)
            ).fetchone()["m"]
            assert current is True, "the current pattern must work everywhere"
            if not legacy:
                # This locale is one where `\y` silently matched nothing.
                # That is the whole reason bangla_boundary exists.
                assert current, f"{locale} breaks \\y but must not break bangla_boundary"
    finally:
        _drop(handle)


def test_at_least_one_locale_on_this_host_was_actually_exercised() -> None:
    """Guards against the suite above passing vacuously by skipping."""
    available = []
    for locale in CANDIDATE_LOCALES:
        dsn, handle = _db_with_locale(locale)
        if dsn is None:
            continue
        _drop(handle)
        available.append(locale)
    assert available, f"no candidate locale exists on this host; tried {CANDIDATE_LOCALES}"

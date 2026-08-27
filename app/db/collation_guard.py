"""Refuse to run against a database whose locale silently breaks Bangla.

WHAT THIS CATCHES, AND WHY IT IS WORTH A STARTUP CHECK

Roughly half this product's corpus is Bangla, and the patterns that
decide whether an article is about a bank, an operator or the exchange
rate are Postgres regexes using `\\y`, the word-boundary escape. See
`app/db/econaxes.py` and `app/db/watchlist.py` for the patterns and for
the long argument about why they are tuned for precision.

`\\y` matches at a transition between a word and a non-word character,
and WHICH CHARACTERS COUNT AS WORD CHARACTERS COMES FROM THE DATABASE'S
LC_CTYPE. Under a UTF-8-aware locale, Bangla letters are word
characters and the patterns behave as designed. Under plain `C` or
`POSIX`, they are not, and every one of those patterns matches NOTHING
in Bangla.

Measured on Postgres 17, one real headline
("ডাচ-বাংলা ব্যাংকের দ্বিতীয় প্রান্তিকে মুনাফা বেড়েছে") against the real
banking pattern:

    C.UTF-8       matches
    en_US.UTF-8   matches
    C             DOES NOT MATCH
    POSIX         DOES NOT MATCH

The failure mode is the reason this is a hard startup check rather than
a logged warning. Nothing errors. Collection runs, articles are stored,
the pipeline completes, the dashboard renders, and every Bangla-derived
number is quietly zero or near it -- indistinguishable from a genuinely
quiet news day. There is no exception to find in a log and no
degradation anyone would notice from the outside, on a product whose
entire job is to report that those numbers are not zero.

It is not hypothetical either: it is how the first CI run of this
project failed. The runner's Postgres container had a non-UTF-8 locale
and two Bangla matching tests failed while every other test passed. The
developer machine that wrote them runs `en_US.UTF-8`, so nothing local
could have caught it, and a managed provider hands you whatever locale
it was provisioned with.
"""

from __future__ import annotations

from app.db.connection import DictConnection
from app.errors import ConfigurationError
from app.logging import get_logger

logger = get_logger(__name__)

# A real headline and the real pattern, not a synthetic pair. If the
# banking axis in app/db/econaxes.py is ever retuned, this should be
# retuned with it -- the point is to exercise the thing production
# actually relies on.
_PROBE_TEXT = "ডাচ-বাংলা ব্যাংকের দ্বিতীয় প্রান্তিকে মুনাফা বেড়েছে"
_PROBE_PATTERN = r"\yব্যাংক\y"

# The counter-example, kept alongside it: `\y` must still REFUSE
# ব্যাংকক (Bangkok). A locale that matched everything would pass a
# bare positive check while being just as wrong in the other direction.
_PROBE_NEGATIVE = "ব্যাংকক পেট এক্সপো শুরু"


def verify_bangla_word_boundaries(conn: DictConnection) -> None:
    """Raise `ConfigurationError` if `\\y` cannot see Bangla word
    boundaries on this database.

    Called from `app.db.migrate` so it runs on every deploy and every
    free-tier cold start, in the same place and for the same reason a
    failed migration stops uvicorn: better to refuse to serve than to
    serve numbers that are wrong in a way nobody can see.
    """
    row = conn.execute(
        "SELECT (%s ~ %s) AS positive, (%s ~ %s) AS negative",
        (_PROBE_TEXT, _PROBE_PATTERN, _PROBE_NEGATIVE, _PROBE_PATTERN),
    ).fetchone()

    if row and row["positive"] and not row["negative"]:
        return

    detail = conn.execute(
        "SELECT datcollate, datctype FROM pg_database WHERE datname = current_database()"
    ).fetchone()
    collate = detail["datcollate"] if detail else "unknown"
    ctype = detail["datctype"] if detail else "unknown"

    logger.error(
        "bangla_word_boundary_check_failed",
        extra={"datcollate": collate, "datctype": ctype},
    )
    raise ConfigurationError(
        f"This database cannot match Bangla word boundaries. Its LC_CTYPE is {ctype!r} "
        f"(LC_COLLATE {collate!r}), under which Postgres does not treat Bangla letters as "
        "word characters, so every `\\y` pattern in app/db/econaxes.py and "
        "app/db/watchlist.py matches nothing. Roughly half the corpus is Bangla, so the "
        "product would run without a single error and report near-zero for every "
        "Bangla-derived figure. Recreate the database with a UTF-8 aware locale "
        "(C.UTF-8 and en_US.UTF-8 are both verified working); C and POSIX are not usable."
    )

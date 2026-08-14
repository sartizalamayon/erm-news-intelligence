"""`collector.editorial_day` — which DATE an article is filed under.

`articles.day` used to be the collection day (whatever date the run
executed on). Because the fetch window is a rolling 30 hours, every run
legitimately picks up the previous evening's news, so 34,130 of 60,058
real articles (56.8%) were filed under a day AFTER they were published.
`day` is now the publication date in Dhaka local time.

The interesting cases are all about the future, and they split two ways:

  * TOMORROW is normal. A newspaper publishes its next-day edition the
    previous evening; 1,222 real rows do exactly that. Rejecting them
    would discard a day of genuine news every night.
  * MONTHS ahead is a parse failure. Two Somoy Journal event pages
    carried the date of the event being announced (41 and 86 days out)
    rather than the publication date.

`FUTURE_TOLERANCE` is the line between those two.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.services.collector import editorial_day
from app.sources._util import DHAKA, FUTURE_TOLERANCE, is_recent

COLLECTED = date(2026, 7, 31)


def test_files_under_the_publication_date_not_the_collection_date():
    """The whole point: an article published yesterday evening and
    collected by a 01:00 run belongs to YESTERDAY.
    """
    published = datetime(2026, 7, 30, 23, 0, tzinfo=DHAKA)
    assert editorial_day(published, collected_on=COLLECTED) == date(2026, 7, 30)


def test_same_day_publication_is_unchanged():
    published = datetime(2026, 7, 31, 9, 0, tzinfo=DHAKA)
    assert editorial_day(published, collected_on=COLLECTED) == COLLECTED


def test_uses_dhaka_local_date_not_utc():
    """19:00 UTC on the 30th is 01:00 Dhaka on the 31st. The desk works
    in Dhaka, so it is the 31st's news.
    """
    published = datetime(2026, 7, 30, 19, 0, tzinfo=timezone.utc)
    assert editorial_day(published, collected_on=COLLECTED) == date(2026, 7, 31)


def test_tomorrows_edition_keeps_tomorrows_date():
    """Real pattern, 1,222 rows: BD Pratidin's 1 August edition is
    published on the evening of 31 July. The edition date is the date a
    reader means.
    """
    published = datetime(2026, 8, 1, 6, 0, tzinfo=DHAKA)
    assert editorial_day(published, collected_on=COLLECTED) == date(2026, 8, 1)


def test_implausibly_future_date_falls_back_to_collection_day():
    """A Somoy Journal event page dated 86 days out is a parse failure,
    not an edition. Filing it in the future would park it permanently at
    the top of every date-sorted view, unreachable by a reviewer working
    through real days.
    """
    published = datetime(2026, 10, 30, 12, 0, tzinfo=DHAKA)
    assert editorial_day(published, collected_on=COLLECTED) == COLLECTED


def test_boundary_of_future_tolerance_is_inclusive():
    at_limit = datetime.combine(COLLECTED + FUTURE_TOLERANCE, datetime.min.time(), tzinfo=DHAKA)
    beyond = at_limit + timedelta(days=1)
    assert editorial_day(at_limit, collected_on=COLLECTED) == at_limit.date()
    assert editorial_day(beyond, collected_on=COLLECTED) == COLLECTED


def test_undated_article_falls_back_to_collection_day():
    """Only 61 of 60,058 real articles have no date, all from one
    incident. When it happens, the collection day is the only honest
    guess — and is better than discarding the article.
    """
    assert editorial_day(None, collected_on=COLLECTED) == COLLECTED


# ── the recency guard that feeds it ──────────────────────────────────────


def test_is_recent_rejects_implausibly_future_dates():
    """`is_recent` tested only `(now - dt) <= WINDOW`. For a future date
    that difference is NEGATIVE, so it was always "recent" — which is
    how event pages dated months ahead got through at all.
    """
    now = datetime.now(DHAKA)
    assert is_recent(now + timedelta(days=60)) is False


def test_is_recent_still_accepts_tomorrows_edition():
    now = datetime.now(DHAKA)
    assert is_recent(now + timedelta(days=1)) is True


def test_is_recent_still_rejects_genuinely_old_articles():
    now = datetime.now(DHAKA)
    assert is_recent(now - timedelta(hours=40)) is False


def test_is_recent_keeps_undated_as_unknown_not_rejected():
    """Three-valued on purpose: None means "no date", which the callers
    keep rather than drop.
    """
    assert is_recent(None) is None

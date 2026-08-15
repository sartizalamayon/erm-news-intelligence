"""app.sources._util — title cleanup, date parsing, window check, URL
normalization. Ported from `backend/collector_logic.py` (legacy
reference, READ-ONLY) — these tests pin the port against the documented
behavior in docs/legacy-behavior.md §2.6.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.sources._util import (
    DHAKA,
    clean_title,
    date_from_url,
    is_recent,
    normalize_url,
    parse_dt,
    today_dhaka,
)


class TestCleanTitle:
    def test_strips_html_tags(self):
        assert clean_title("<a href='x'>Hello <b>World</b></a>") == "Hello World"

    def test_unescapes_entities(self):
        assert clean_title("Fish &amp; Chips &#039;fresh&#039;") == "Fish & Chips 'fresh'"

    def test_collapses_whitespace(self):
        assert clean_title("  multiple   \n\t spaces  ") == "multiple spaces"

    def test_empty_and_none(self):
        assert clean_title("") == ""
        assert clean_title(None) == ""

    def test_plain_text_passthrough(self):
        assert clean_title("Plain headline, no markup") == "Plain headline, no markup"

    def test_unescapes_entities_beyond_the_common_seven(self):
        """Entity decoding used to be a hand-written map of seven, so
        everything else was stored verbatim and DISPLAYED as its code:
        220 stored headlines read `labels UK &#8216;Islamic Republic of
        Britain&#8217;` because the frontend escapes titles (correctly,
        they are remote input) rather than rendering them.
        """
        assert (
            clean_title("Israeli PM Netanyahu labels UK &#8216;Islamic Republic of Britain&#8217;")
            == "Israeli PM Netanyahu labels UK ‘Islamic Republic of Britain’"
        )
        assert clean_title("Gates&rsquo; startup &lsquo;Phia&rsquo;") == "Gates’ startup ‘Phia’"
        # Zero-width joiner: Bengali conjuncts need it and it is invisible,
        # so a raw `&zwj;` in the middle of a word is especially jarring.
        assert clean_title("র&zwj;্যাগিং") == "র‍্যাগিং"
        assert clean_title("a &#8230; b") == "a … b"

    def test_decodes_exactly_one_level(self):
        """The old map iterated a dict, decoding `&amp;` first, so
        `&amp;lt;` became `&lt;` and then `<`. A double decode turns
        deliberately-escaped text back into markup, which is the wrong
        direction for anything downstream that trusts this output.
        """
        assert clean_title("&amp;lt;script&amp;gt;") == "&lt;script&gt;"
        assert clean_title("&amp;amp;") == "&amp;"

    def test_leaves_a_non_entity_ampersand_alone(self):
        assert clean_title("AT&T and R&D") == "AT&T and R&D"


class TestParseDt:
    def test_epoch_seconds(self):
        dt = parse_dt(1735689600)  # 2025-01-01T00:00:00Z
        assert dt is not None
        assert dt.tzinfo is not None

    def test_epoch_milliseconds(self):
        dt_ms = parse_dt(1735689600000)
        dt_s = parse_dt(1735689600)
        assert dt_ms == dt_s

    def test_iso_with_z(self):
        dt = parse_dt("2026-06-19T12:38:30Z")
        assert dt is not None
        assert dt.year == 2026 and dt.month == 6 and dt.day == 19

    def test_rfc2822(self):
        dt = parse_dt("Fri, 19 Jun 2026 12:38:30 GMT")
        assert dt is not None
        assert dt.year == 2026

    def test_unparseable_returns_none(self):
        assert parse_dt("not a date at all") is None

    def test_none_and_empty(self):
        assert parse_dt(None) is None
        assert parse_dt("") is None


class TestDateFromUrl:
    def test_slash_separated(self):
        dt = date_from_url("https://example.com/2026/06/16/some-article")
        assert dt == datetime(2026, 6, 16, tzinfo=timezone.utc)

    def test_dash_separated(self):
        dt = date_from_url("https://example.com/news/2026-06-16-headline")
        assert dt == datetime(2026, 6, 16, tzinfo=timezone.utc)

    def test_no_date_in_url(self):
        assert date_from_url("https://example.com/news/12345") is None

    def test_none_url(self):
        assert date_from_url(None) is None


class TestIsRecent:
    def test_within_window(self):
        now = datetime(2026, 8, 6, 10, 0, tzinfo=DHAKA)
        dt = now - timedelta(hours=5)
        assert is_recent(dt, now=now) is True

    def test_outside_window(self):
        now = datetime(2026, 8, 6, 10, 0, tzinfo=DHAKA)
        dt = now - timedelta(hours=40)
        assert is_recent(dt, now=now) is False

    def test_exactly_on_boundary_is_inside(self):
        now = datetime(2026, 8, 6, 10, 0, tzinfo=DHAKA)
        dt = now - timedelta(hours=30)
        assert is_recent(dt, now=now) is True

    def test_none_is_none(self):
        assert is_recent(None) is None

    def test_naive_datetime_treated_as_utc(self):
        now = datetime(2026, 8, 6, 10, 0, tzinfo=DHAKA)
        naive = (now - timedelta(hours=1)).astimezone(timezone.utc).replace(tzinfo=None)
        assert is_recent(naive, now=now) is True

    def test_uses_fresh_now_by_default(self):
        """Regression guard for legacy-behavior.md §7 item 11: NOW must
        never be a module-level constant frozen at import time. Calling
        is_recent() with no `now=` override must reflect the actual
        current wall clock, not a stale value.
        """
        just_now = datetime.now(DHAKA) - timedelta(minutes=1)
        assert is_recent(just_now) is True


class TestNormalizeUrl:
    def test_drops_www_and_forces_https(self):
        assert normalize_url("http://www.example.com/a/b") == "https://example.com/a/b"

    def test_strips_trailing_slash(self):
        assert normalize_url("https://example.com/a/b/") == "https://example.com/a/b"

    def test_strips_tracking_params(self):
        result = normalize_url("https://example.com/a?utm_source=x&utm_medium=y&id=5")
        assert result == "https://example.com/a?id=5"

    def test_strips_fragment(self):
        assert normalize_url("https://example.com/a#section") == "https://example.com/a"

    def test_unwraps_google_news_redirect(self):
        wrapped = "https://news.google.com/rss/articles/xyz?url=https%3A%2F%2Fwww.real-site.com%2Farticle"
        assert normalize_url(wrapped) == "https://real-site.com/article"

    def test_empty_url(self):
        assert normalize_url("") == ""

    def test_same_article_different_query_noise_dedupes_identically(self):
        a = normalize_url("https://www.site.com/story?utm_source=fb&fbclid=abc123")
        b = normalize_url("https://site.com/story/")
        assert a == b


def test_today_dhaka_returns_a_date():
    d = today_dhaka()
    assert d.year >= 2026

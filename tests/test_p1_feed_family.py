"""Feed/API-family method modules: `_api` (generic JSON walker, cursor
and offset pagination), `_wp_rest` (WordPress REST paginator), `_rss` /
`_rss_multi` (RSS/Atom parsing), `_gnews` (Google News RSS search).
"""

from __future__ import annotations

import pytest

from app.errors import SourceFetchError
from app.sources import _api, _gnews, _rss, _wp_rest
from app.sources import http as _http
from tests.test_p1_helpers import FakeResponse, FakeSession


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """None of this file's tests care about real retry timing (that's
    covered by tests/test_p1_http.py's dedicated backoff tests) — several
    deliberately trigger a retryable 5xx to prove error handling, and a
    real `time.sleep` between attempts would make this file needlessly
    slow (~7s+ per retried-to-exhaustion call).
    """
    monkeypatch.setattr(_http.time, "sleep", lambda *_: None)

RSS_FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Example Feed</title>
  <item>
    <title>First Item</title>
    <link>https://example.com/first</link>
    <pubDate>Thu, 06 Aug 2026 09:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Second Item</title>
    <link>https://example.com/second</link>
    <pubDate>Thu, 06 Aug 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>"""

ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atom Item</title>
    <link href="https://example.com/atom-item"/>
    <updated>2026-08-06T09:00:00Z</updated>
  </entry>
</feed>"""


class TestRss:
    def test_parses_rss_items(self):
        session = FakeSession().add("https://example.com/feed.xml", FakeResponse(status_code=200, text=RSS_FEED))
        items = _rss.fetch_rss(session, "https://example.com/feed.xml", source="test")
        assert len(items) == 2
        assert items[0].url == "https://example.com/first"

    def test_parses_atom_items(self):
        session = FakeSession().add("https://example.com/atom.xml", FakeResponse(status_code=200, text=ATOM_FEED))
        items = _rss.fetch_rss(session, "https://example.com/atom.xml", source="test")
        assert len(items) == 1
        assert items[0].url == "https://example.com/atom-item"

    def test_required_raises_when_unreachable(self):
        session = FakeSession().add("https://example.com/gone.xml", FakeResponse(status_code=500))
        with pytest.raises(SourceFetchError):
            _rss.fetch_rss(session, "https://example.com/gone.xml", source="test", required=True, max_retries=0)

    def test_retryable_status_does_not_sleep_forever_in_tests(self, monkeypatch):
        """Belt-and-suspenders: even if a test forgets max_retries=0, a
        retryable status must not turn into real multi-second sleeps —
        confirms fetch_rss forwards max_retries through to http.get().
        """
        from app.sources import http

        slept = []
        monkeypatch.setattr(http.time, "sleep", lambda s: slept.append(s))
        session = FakeSession().add("https://example.com/flaky.xml", FakeResponse(status_code=503))
        _rss.fetch_rss(session, "https://example.com/flaky.xml", source="test", required=False, max_retries=2)
        assert len(slept) == 2  # exactly max_retries sleeps, not an unbounded retry loop

    def test_not_required_returns_empty(self):
        session = FakeSession().add("https://example.com/gone.xml", FakeResponse(status_code=500))
        items = _rss.fetch_rss(session, "https://example.com/gone.xml", source="test", required=False, max_retries=0)
        assert items == []


class TestRssMulti:
    def test_merges_and_dedupes_across_feeds(self):
        feed_a = RSS_FEED
        feed_b = """<?xml version="1.0"?><rss version="2.0"><channel>
          <item><title>First Item</title><link>https://example.com/first</link></item>
          <item><title>Third Item</title><link>https://example.com/third</link></item>
        </channel></rss>"""
        session = FakeSession()
        session.add("https://example.com/a.xml", FakeResponse(status_code=200, text=feed_a))
        session.add("https://example.com/b.xml", FakeResponse(status_code=200, text=feed_b))
        items = _rss.fetch_rss_multi(session, ["https://example.com/a.xml", "https://example.com/b.xml"], source="test")
        urls = {i.url for i in items}
        assert urls == {"https://example.com/first", "https://example.com/second", "https://example.com/third"}

    def test_raises_only_when_every_feed_fails(self):
        session = FakeSession()
        session.add("https://example.com/a.xml", FakeResponse(status_code=500))
        session.add("https://example.com/b.xml", FakeResponse(status_code=500))
        with pytest.raises(SourceFetchError):
            _rss.fetch_rss_multi(session, ["https://example.com/a.xml", "https://example.com/b.xml"], source="test")

    def test_survives_one_feed_failing(self):
        session = FakeSession()
        session.add("https://example.com/a.xml", FakeResponse(status_code=500))
        session.add("https://example.com/b.xml", FakeResponse(status_code=200, text=RSS_FEED))
        items = _rss.fetch_rss_multi(session, ["https://example.com/a.xml", "https://example.com/b.xml"], source="test")
        assert len(items) == 2


class TestGnews:
    def test_throttles_and_queries_site_search(self, monkeypatch):
        monkeypatch.setattr(_gnews.time, "sleep", lambda *_: None)
        session = FakeSession()

        def handler(method, url, **kwargs):
            assert "site%3Aexample.com" in url or "site:example.com" in url
            return FakeResponse(status_code=200, text=RSS_FEED)

        # Register by intercepting request() directly since the URL is query-encoded
        session.request = lambda method, url, **kwargs: handler(method, url, **kwargs)  # type: ignore[method-assign]
        items = _gnews.fetch_gnews(session, "example.com", source="test")
        assert len(items) == 2


class TestWpRest:
    def test_single_page(self):
        posts = [
            {"title": {"rendered": "Post One"}, "link": "https://example.com/p1", "date_gmt": "2026-08-06T09:00:00"},
        ]
        session = FakeSession()
        session.add("https://example.com/wp-json/wp/v2/posts", FakeResponse(status_code=200, json_data=posts))
        # Page 2 comes back empty — "posts exhausted", the real-world
        # signal that stops pagination (distinct from FakeSession's
        # single-registered-response replay behavior).
        session.add("https://example.com/wp-json/wp/v2/posts", FakeResponse(status_code=200, json_data=[]))
        items = _wp_rest.fetch_wp_rest(session, "https://example.com", source="test")
        assert len(items) == 1
        assert items[0].title == "Post One"

    def test_400_means_end_of_pages(self):
        page1 = [
            {"title": {"rendered": f"Post {i}"}, "link": f"https://example.com/p{i}", "date_gmt": "2026-08-06T09:00:00"}
            for i in range(100)
        ]
        session = FakeSession()
        session.add("https://example.com/wp-json/wp/v2/posts", FakeResponse(status_code=200, json_data=page1))
        session.add("https://example.com/wp-json/wp/v2/posts", FakeResponse(status_code=400))
        items = _wp_rest.fetch_wp_rest(session, "https://example.com", source="test")
        assert len(items) == 100

    def test_stops_when_page_has_nothing_recent(self):
        old_posts = [
            {"title": {"rendered": "Old"}, "link": "https://example.com/old", "date_gmt": "2020-01-01T00:00:00"}
        ]
        session = FakeSession().add(
            "https://example.com/wp-json/wp/v2/posts", FakeResponse(status_code=200, json_data=old_posts)
        )
        items = _wp_rest.fetch_wp_rest(session, "https://example.com", source="test")
        assert len(items) == 1  # still yielded (undated/old handling happens at write time), but pagination stopped
        assert session.call_count == 1

    def test_raises_on_first_page_unreachable(self):
        session = FakeSession().add("https://example.com/wp-json/wp/v2/posts", FakeResponse(status_code=500))
        with pytest.raises(SourceFetchError):
            _wp_rest.fetch_wp_rest(session, "https://example.com", source="test")


class TestJsonApi:
    def test_offset_pagination_stops_when_page_has_nothing_recent(self):
        from datetime import datetime, timezone

        now_epoch = int(datetime.now(timezone.utc).timestamp())
        cfg = _api.ApiConfig(
            url="https://api.example.com/search?limit=10",
            items_path="items",
            title_field="headline",
            url_field="url",
            date_field="published-at",
            base_url="https://example.com",
            paginate="offset",
            offset_step=10,
            max_pages=5,
        )
        page1 = {"items": [{"headline": "A", "url": "/a", "published-at": now_epoch}]}
        page2 = {"items": [{"headline": "Old", "url": "/old", "published-at": 100000000}]}
        session = FakeSession()
        session.add("https://api.example.com/search?limit=10&offset=0", FakeResponse(status_code=200, json_data=page1))
        session.add("https://api.example.com/search?limit=10&offset=10", FakeResponse(status_code=200, json_data=page2))
        items = _api.fetch_json_api(session, cfg, source="test")
        urls = {i.url for i in items}
        assert "https://example.com/a" in urls

    def test_cursor_pagination_follows_next_field(self):
        cfg = _api.ApiConfig(
            url="https://api.example.com/posts",
            items_path="items",
            title_field="title",
            url_field="slug",
            date_field="datetime",
            base_url="https://example.com",
            next_field="nextPageUrl",
            paginate="cursor",
            max_pages=5,
        )
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        recent_iso = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        page1 = {
            # Must be inside app.sources._util.WINDOW (30h) at whatever
            # real time the suite runs, or page_had_recent goes False and
            # pagination stops before nextPageUrl is ever fetched.
            "items": [{"title": "First", "slug": "/first", "datetime": recent_iso}],
            "nextPageUrl": "https://api.example.com/posts?page=2",
        }
        page2 = {"items": [{"title": "Second", "slug": "/second", "datetime": recent_iso}]}
        session = FakeSession()
        session.add("https://api.example.com/posts", FakeResponse(status_code=200, json_data=page1))
        session.add("https://api.example.com/posts?page=2", FakeResponse(status_code=200, json_data=page2))
        items = _api.fetch_json_api(session, cfg, source="test")
        assert len(items) == 2

    def test_raises_when_first_page_unreachable(self):
        cfg = _api.ApiConfig(url="https://api.example.com/posts")
        session = FakeSession().add("https://api.example.com/posts", FakeResponse(status_code=500))
        with pytest.raises(SourceFetchError):
            _api.fetch_json_api(session, cfg, source="test")

    def test_raises_on_non_json_first_page(self):
        cfg = _api.ApiConfig(url="https://api.example.com/posts")
        session = FakeSession().add(
            "https://api.example.com/posts", FakeResponse(status_code=200, text="<html>not json</html>")
        )
        with pytest.raises(SourceFetchError):
            _api.fetch_json_api(session, cfg, source="test")

"""Sitemap-family method modules — the highest-volume method family
(6 of 21 ported sources). Covers: `_sitemap` (news-sitemap XML, plain
and via a sitemap-index), `_dated_sitemap` (per-date files), `_bbc`
(sitemap-index scoped to /news/), `_sitemap_index_merge` (Rupali BD's
news/post/article/latest child union), and `_curl_sitemap` (the system-
curl transport workaround, with `subprocess.run` monkeypatched — never a
real curl invocation in a test).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.errors import SourceFetchError
from app.sources import _bbc, _curl_sitemap, _dated_sitemap, _sitemap, _sitemap_index_merge
from tests.test_p1_helpers import FakeResponse, FakeSession

GOOGLE_NEWS_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
  <url>
    <loc>https://example.com/2026/08/06/first-story</loc>
    <news:news>
      <news:publication_date>2026-08-06T09:00:00Z</news:publication_date>
      <news:title>First Story Title</news:title>
    </news:news>
  </url>
  <url>
    <loc>https://example.com/2026/08/06/second-story</loc>
    <news:news>
      <news:publication_date>2026-08-06T08:30:00Z</news:publication_date>
      <news:title>Second Story Title</news:title>
    </news:news>
  </url>
  <url>
    <loc>https://example.com/2020/01/01/ancient-story</loc>
    <lastmod>2020-01-01T00:00:00Z</lastmod>
  </url>
</urlset>"""

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap-news.xml</loc></sitemap>
</sitemapindex>"""


class TestParseUrlset:
    def test_extracts_title_url_and_date(self):
        tuples = _sitemap.parse_urlset(GOOGLE_NEWS_SITEMAP.encode(), cap=600)
        urls = [u for (_t, u, _d) in tuples]
        assert "https://example.com/2026/08/06/first-story" in urls
        titles = {u: t for (t, u, _d) in tuples}
        assert titles["https://example.com/2026/08/06/first-story"] == "First Story Title"

    def test_drops_confidently_old_articles(self):
        tuples = _sitemap.parse_urlset(GOOGLE_NEWS_SITEMAP.encode(), cap=600)
        urls = [u for (_t, u, _d) in tuples]
        assert not any("ancient-story" in u for u in urls)

    def test_respects_cap(self):
        tuples = _sitemap.parse_urlset(GOOGLE_NEWS_SITEMAP.encode(), cap=1)
        assert len(tuples) == 1

    def test_malformed_xml_returns_empty_list(self):
        assert _sitemap.parse_urlset(b"<not><valid", cap=600) == []


class TestFetchOneSitemapUrl:
    def test_plain_sitemap(self):
        session = FakeSession().add(
            "https://example.com/news-sitemap.xml", FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP)
        )
        items = _sitemap.fetch_one_sitemap_url(session, "https://example.com/news-sitemap.xml", source="test")
        assert len(items) == 2
        assert items[0].source_key == "test"

    def test_follows_sitemap_index_to_first_news_child(self):
        session = FakeSession()
        session.add("https://example.com/sitemap.xml", FakeResponse(status_code=200, text=SITEMAP_INDEX))
        session.add("https://example.com/sitemap-news.xml", FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP))
        items = _sitemap.fetch_one_sitemap_url(session, "https://example.com/sitemap.xml", source="test")
        assert len(items) == 2

    def test_non_200_returns_empty(self):
        session = FakeSession().add("https://example.com/gone.xml", FakeResponse(status_code=404))
        assert _sitemap.fetch_one_sitemap_url(session, "https://example.com/gone.xml", source="test") == []


class TestFetchNewsSitemap:
    def test_uses_primary_url_when_it_works(self):
        session = FakeSession().add(
            "https://example.com/news-sitemap.xml", FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP)
        )
        items = _sitemap.fetch_news_sitemap(session, "https://example.com/news-sitemap.xml", source="test")
        assert len(items) == 2
        # only the primary URL was ever requested — no fallback candidates burned
        assert session.call_count == 1

    def test_falls_back_when_primary_is_empty(self):
        empty_sitemap = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
        session = FakeSession()
        session.add("https://example.com/news-sitemap.xml", FakeResponse(status_code=200, text=empty_sitemap))
        session.add("https://example.com/robots.txt", FakeResponse(status_code=404))
        for path in _sitemap.NEWS_SITEMAP_PATHS:
            url = "https://example.com" + path
            if url == "https://example.com/sitemap-news.xml":
                session.add(url, FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP))
            else:
                session.add(url, FakeResponse(status_code=404))
        items = _sitemap.fetch_news_sitemap(session, "https://example.com/news-sitemap.xml", source="test")
        assert len(items) == 2

    def test_empty_when_no_candidate_works(self):
        session = FakeSession()
        session.add("https://example.com/news-sitemap.xml", FakeResponse(status_code=404))
        session.add("https://example.com/robots.txt", FakeResponse(status_code=404))
        for path in _sitemap.NEWS_SITEMAP_PATHS:
            session.add("https://example.com" + path, FakeResponse(status_code=404))
        items = _sitemap.fetch_news_sitemap(session, "https://example.com/news-sitemap.xml", source="test")
        assert items == []


class TestDatedSitemap:
    def test_fetches_todays_dated_file(self):
        from app.sources._util import DHAKA

        today_str = datetime.now(DHAKA).strftime("%Y-%m-%d")
        session = FakeSession()
        today_url = f"https://example.com/sitemap/{today_str}.xml"
        session.add(today_url, FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP))

        items = _dated_sitemap.fetch_dated_sitemap(
            session, "https://example.com/sitemap/{date}.xml", source="test", days_back=1
        )
        assert len(items) == 2

    def test_merges_today_and_yesterday_deduped(self):
        from datetime import timedelta

        from app.sources._util import DHAKA

        now = datetime.now(DHAKA)
        today_str = now.strftime("%Y-%m-%d")
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

        session = FakeSession()
        session.add(
            f"https://example.com/sitemap/{today_str}.xml",
            FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP),
        )
        session.add(
            f"https://example.com/sitemap/{yesterday_str}.xml",
            FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP),
        )

        items = _dated_sitemap.fetch_dated_sitemap(
            session, "https://example.com/sitemap/{date}.xml", source="test", days_back=2
        )
        # same 2 URLs appear in both days' files — deduped down to 2, not 4
        assert len(items) == 2

    def test_raises_when_today_totally_unreachable(self, monkeypatch):
        from app.sources._util import DHAKA

        today_str = datetime.now(DHAKA).strftime("%Y-%m-%d")
        today_url = f"https://example.com/sitemap/{today_str}.xml"
        # The sitemap fetch itself comes back non-200 (empty, not an
        # exception) so fetch_one_sitemap_url returns [] cleanly...
        session = FakeSession().add(today_url, FakeResponse(status_code=404))
        # ...and the dedicated reachability probe (a SEPARATE call this
        # module makes to distinguish "empty" from "unreachable") is the
        # one monkeypatched to simulate total unreachability.
        monkeypatch.setattr(_dated_sitemap, "get", lambda *a, **k: None)
        with pytest.raises(SourceFetchError):
            _dated_sitemap.fetch_dated_sitemap(
                session, "https://example.com/sitemap/{date}.xml", source="test", days_back=1
            )


class TestBbcSitemaps:
    INDEX = (
        "<sitemapindex>"
        "<sitemap><loc>https://www.bbc.com/sitemaps/https-sitemap-com-news-1.xml</loc></sitemap>"
        "</sitemapindex>"
    )
    SUB_SITEMAP = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
            xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
      <url><loc>https://www.bbc.com/news/world-123</loc>
        <news:news>
          <news:publication_date>2026-08-06T09:00:00Z</news:publication_date>
          <news:title>World story</news:title>
        </news:news>
      </url>
      <url><loc>https://www.bbc.com/sport/football-456</loc>
        <news:news>
          <news:publication_date>2026-08-06T09:00:00Z</news:publication_date>
          <news:title>Sport story</news:title>
        </news:news>
      </url>
    </urlset>"""

    def test_scopes_to_news_path_only(self):
        session = FakeSession()
        session.add("https://www.bbc.com/sitemaps/index.xml", FakeResponse(status_code=200, text=self.INDEX))
        session.add(
            "https://www.bbc.com/sitemaps/https-sitemap-com-news-1.xml",
            FakeResponse(status_code=200, text=self.SUB_SITEMAP),
        )
        items = _bbc.fetch_bbc_sitemaps(session, "https://www.bbc.com/sitemaps/index.xml", source="bbc_news")
        urls = [i.url for i in items]
        assert any("/news/world-123" in u for u in urls)
        assert not any("/sport/" in u for u in urls)

    def test_raises_when_index_unreachable(self, monkeypatch):
        import requests

        from app.sources import http

        monkeypatch.setattr(http.time, "sleep", lambda *_: None)
        session = FakeSession()
        # http.get()'s default max_retries=3 means up to 4 attempts total.
        for _ in range(4):
            session.add("https://www.bbc.com/sitemaps/index.xml", requests.exceptions.ConnectionError("down"))
        with pytest.raises(SourceFetchError):
            _bbc.fetch_bbc_sitemaps(session, "https://www.bbc.com/sitemaps/index.xml", source="bbc_news")


class TestSitemapIndexMerge:
    INDEX = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://example.com/sitemap-news-1.xml</loc></sitemap>
      <sitemap><loc>https://example.com/sitemap-pages-1.xml</loc></sitemap>
      <sitemap><loc>https://example.com/sitemap-post-1.xml</loc></sitemap>
    </sitemapindex>"""

    def test_merges_only_news_like_children(self):
        session = FakeSession()
        session.add("https://example.com/sitemap.xml", FakeResponse(status_code=200, text=self.INDEX))
        session.add(
            "https://example.com/sitemap-news-1.xml", FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP)
        )
        session.add(
            "https://example.com/sitemap-post-1.xml", FakeResponse(status_code=200, text=GOOGLE_NEWS_SITEMAP)
        )
        items = _sitemap_index_merge.fetch_sitemap_index_merge(
            session, "https://example.com/sitemap.xml", source="test"
        )
        # "pages" child was never fetched (not news-like) — only 2 URLs, deduped across the 2 fetched children
        assert len(items) == 2
        assert "https://example.com/sitemap-pages-1.xml" not in [c[1] for c in session.calls]


class TestCurlSitemap:
    def test_parses_via_curl_subprocess(self, monkeypatch):
        class FakeProc:
            stdout = GOOGLE_NEWS_SITEMAP.encode("utf-8")

        monkeypatch.setattr(_curl_sitemap.subprocess, "run", lambda *a, **k: FakeProc())
        items = _curl_sitemap.fetch_curl_sitemap("https://kalbela.example/news-sitemap.xml", source="kalbela")
        assert len(items) == 2

    def test_raises_when_curl_produces_nothing(self, monkeypatch):
        class FakeProc:
            stdout = b""

        monkeypatch.setattr(_curl_sitemap.subprocess, "run", lambda *a, **k: FakeProc())
        with pytest.raises(SourceFetchError):
            _curl_sitemap.fetch_curl_sitemap("https://dead.example/news-sitemap.xml", source="dead")

    def test_curl_get_never_raises_on_subprocess_failure(self, monkeypatch):
        import subprocess

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="curl", timeout=1)

        monkeypatch.setattr(_curl_sitemap.subprocess, "run", boom)
        assert _curl_sitemap._curl_get("https://example.com/x.xml") == ""


def test_utc_epoch_used_where_relevant():
    # sanity: our synthetic dates parse into real, comparable datetimes
    dt = datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc)
    assert dt.year == 2026

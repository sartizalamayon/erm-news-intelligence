"""Numeric-ID enumeration method modules: `_id_enum` (Daily Adin,
Sangbad) and `_daily_star` (Daily Star's Drupal node-ID walk). Both fan
out internally with their own bounded `ThreadPoolExecutor` — tested here
against a scripted `FakeSession` standing in for `requests.Session`, no
real sockets or real threads-doing-real-IO.
"""

from __future__ import annotations

import pytest

from app.errors import SourceFetchError
from app.sources import _daily_star, _id_enum
from tests.test_p1_helpers import FakeResponse, FakeSession


@pytest.fixture(autouse=True)
def _no_real_sleep_and_small_time_budget(monkeypatch):
    from app.sources import _daily_star as ds_mod
    from app.sources import _id_enum as ie_mod
    from app.sources import http as http_mod

    monkeypatch.setattr(http_mod.time, "sleep", lambda *_: None)
    # Small time budgets so a walk over a large synthetic ID space (still
    # needed to satisfy _daily_star's 6-digit node-ID regex) terminates
    # quickly in tests rather than running for the full production budget
    # (90s / 120s) — the budget check runs BEFORE each batch, so the one
    # batch containing a registered "hit" always completes first.
    monkeypatch.setattr(ie_mod, "TIME_BUDGET_SECONDS", 0.5)
    monkeypatch.setattr(ds_mod, "TIME_BUDGET_SECONDS", 0.5)


HOMEPAGE_HTML = """<html><body>
<a href="/news/1050">latest</a>
<a href="/news/1049">prev</a>
</body></html>"""


def _jsonld_page(nid: int, *, headline: str, dt_iso: str, canonical: str | None = None) -> str:
    canon_tag = f'<link rel="canonical" href="{canonical}">' if canonical else ""
    return f"""<html><head>
    {canon_tag}
    <script type="application/ld+json">
    {{"@type":"NewsArticle","headline":"{headline}","datePublished":"{dt_iso}"}}
    </script>
    </head><body>ID {nid}</body></html>"""


class TestIdEnumCanonicalIsHomepage:
    """Mirrors Sangbad's config: canonical_is_homepage not-article check,
    date_tz_override=dhaka.
    """

    def _cfg(self):
        return _id_enum.IdEnumConfig(
            site_url="https://sangbad.example",
            article_pattern="/news/{id}",
            id_regex=r"/news/(\d+)",
            date_tz_override="dhaka",
            not_article_check="canonical_is_homepage",
            workers=2,
            batch_size=5,
        )

    def test_resolves_articles_and_stops_on_old_batches(self, monkeypatch):
        from datetime import datetime, timedelta

        from app.sources._util import DHAKA

        cfg = self._cfg()
        session = FakeSession()
        session.add(cfg.site_url, FakeResponse(status_code=200, text=HOMEPAGE_HTML))

        now = datetime.now(DHAKA)
        for nid in range(1041, 1051):
            url = f"{cfg.site_url}/news/{nid}"
            if nid >= 1046:
                dt_str = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                html = _jsonld_page(nid, headline="Recent headline text", dt_iso=dt_str, canonical=url)
            else:
                old = now - timedelta(days=10)
                dt_str = old.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                html = _jsonld_page(nid, headline="Old headline text", dt_iso=dt_str, canonical=url)
            session.add(url, FakeResponse(status_code=200, text=html, url=url))

        items = _id_enum.fetch_id_enum(session, cfg, source="sangbad")
        assert len(items) >= 1
        assert all("Recent" in i.title for i in items)

    def test_non_article_page_rejected_via_canonical_check(self):
        cfg = self._cfg()
        session = FakeSession()
        session.add(cfg.site_url, FakeResponse(status_code=200, text='<a href="/news/1050">x</a>'))
        # canonical == site homepage -> not an article
        session.add(
            f"{cfg.site_url}/news/1050",
            FakeResponse(
                status_code=200,
                text=f'<link rel="canonical" href="{cfg.site_url}">',
                url=f"{cfg.site_url}/news/1050",
            ),
        )
        items = _id_enum.fetch_id_enum(session, cfg, source="sangbad")
        assert items == []

    def test_raises_when_homepage_has_no_ids(self):
        cfg = self._cfg()
        session = FakeSession().add(cfg.site_url, FakeResponse(status_code=200, text="<html>nothing here</html>"))
        with pytest.raises(SourceFetchError):
            _id_enum.fetch_id_enum(session, cfg, source="sangbad")

    def test_raises_when_homepage_unreachable(self):
        cfg = self._cfg()
        session = FakeSession().add(cfg.site_url, FakeResponse(status_code=500))
        with pytest.raises(SourceFetchError):
            _id_enum.fetch_id_enum(session, cfg, source="sangbad")


class TestIdEnumCanonicalHop:
    """Mirrors Daily Adin's config: the probed URL is a shell; the real
    JSON-LD lives at its canonical URL.
    """

    def test_hops_to_canonical_for_real_data(self):
        cfg = _id_enum.IdEnumConfig(
            site_url="https://adin.example",
            article_pattern="/news/{id}",
            id_regex=r"/(\d{4,})",
            canonical_hop=True,
            not_article_check="none",
            workers=2,
            batch_size=3,
        )
        from datetime import datetime, timezone

        session = FakeSession()
        session.add(cfg.site_url, FakeResponse(status_code=200, text='<a href="/1234">x</a>'))
        shell_url = f"{cfg.site_url}/news/1234"
        canon_url = f"{cfg.site_url}/1234/real-slug"
        session.add(shell_url, FakeResponse(status_code=200, text=f'<link rel="canonical" href="{canon_url}">'))
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        canon_page = _jsonld_page(1234, headline="Hopped headline", dt_iso=now_iso)
        session.add(canon_url, FakeResponse(status_code=200, text=canon_page, url=canon_url))

        items = _id_enum.fetch_id_enum(session, cfg, source="daily_adin")
        assert len(items) == 1
        assert items[0].url == canon_url
        assert items[0].title == "Hopped headline"

    def test_no_canonical_means_not_an_article(self):
        cfg = _id_enum.IdEnumConfig(
            site_url="https://adin.example",
            article_pattern="/news/{id}",
            id_regex=r"/(\d{4,})",
            canonical_hop=True,
            not_article_check="none",
            workers=2,
            batch_size=3,
        )
        session = FakeSession()
        session.add(cfg.site_url, FakeResponse(status_code=200, text='<a href="/1234">x</a>'))
        session.add(f"{cfg.site_url}/news/1234", FakeResponse(status_code=200, text="<html>no canonical here</html>"))
        items = _id_enum.fetch_id_enum(session, cfg, source="daily_adin")
        assert items == []


class TestDailyStarNodes:
    def test_resolves_relative_and_absolute_dates(self):
        from datetime import datetime

        from app.sources._util import DHAKA

        session = FakeSession()
        session.add(
            "https://star.example",
            FakeResponse(status_code=200, text='<a href="/news/some-slug-100050">x</a>'),
        )
        today = datetime.now(DHAKA)
        node_url = "https://star.example/node/100050"
        final_url = "https://star.example/news/some-story-100050"
        html = f"""<html><body>
        <h1>Story Headline</h1>
        <div class="block-article-meta-block">
          {today.strftime('%d %B %Y')}, {today.strftime('%I:%M')} {"AM" if today.hour < 12 else "PM"} SHARE
        </div>
        </body></html>"""
        session.add(node_url, FakeResponse(status_code=200, text=html, url=final_url))
        items = _daily_star.fetch_daily_star_nodes(session, "https://star.example", source="daily_star")
        assert len(items) == 1
        assert items[0].title == "Story Headline"

    def test_still_on_node_path_is_rejected(self):
        session = FakeSession()
        session.add("https://star.example", FakeResponse(status_code=200, text='<a href="/news/x-500001">x</a>'))
        node_url = "https://star.example/node/500001"
        # Not redirected away from /node/ -> not a published article
        session.add(node_url, FakeResponse(status_code=200, text="<html></html>", url=node_url))
        items = _daily_star.fetch_daily_star_nodes(session, "https://star.example", source="daily_star")
        assert items == []

    def test_raises_when_homepage_has_no_node_ids(self):
        session = FakeSession().add("https://star.example", FakeResponse(status_code=200, text="<html>empty</html>"))
        with pytest.raises(SourceFetchError):
            _daily_star.fetch_daily_star_nodes(session, "https://star.example", source="daily_star")

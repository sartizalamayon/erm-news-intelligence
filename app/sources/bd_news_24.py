"""BD News 24 — Bengali/English general daily. #23 by article volume.
News-sitemap. Legacy also configured a confirmed-working `fallback_rss`
(from an OPML export); wired here as a genuine empty-result fallback
(speed-techniques.md §4 point 4). Config ported from
`backend/collector_logic.py` SOURCES list (:98-102).

Uses `require_news_sitemap` (not the bare `fetch_news_sitemap`) for the
primary — see app/sources/ajker_patrika.py's module docstring for the
full rationale (docs/red-team-findings.md RT-2): the RSS fallback still
fires on any empty sitemap result, but only a confirmed-dead sitemap
PLUS an empty/unreachable RSS fallback raises, so a totally dead host
is reported `status="failed"`, not indistinguishable from "healthy, no
news today".
"""

from __future__ import annotations

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._body import fetch_generic_article_body
from app.sources._rss import fetch_rss
from app.sources._sitemap import require_news_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

logger = get_logger(__name__)

SITEMAP_URL = "https://bdnews24.com/news_sitemap.xml"
FALLBACK_RSS_URL = "https://bdnews24.com/?widgetName=rssfeed&widgetId=1150&getXmlFeed=true"


@register
class BdNews24Source(NewsSource):
    key = "bd_news_24"
    display_name = "BD News 24"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        try:
            items = require_news_sitemap(self.session, SITEMAP_URL, source=self.key)
            sitemap_confirmed_dead = False
        except SourceFetchError:
            items = []
            sitemap_confirmed_dead = True

        if items:
            return items

        logger.warning(
            "source_fallback_triggered",
            extra={"source": self.key, "primary_method": "sitemap", "fallback_method": "rss"},
        )
        rss_items = fetch_rss(self.session, FALLBACK_RSS_URL, source=self.key, required=False)
        if rss_items:
            return rss_items

        if sitemap_confirmed_dead:
            raise SourceFetchError(
                f"sitemap and rss fallback both unreachable: {SITEMAP_URL}", source=self.key
            )
        return []

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

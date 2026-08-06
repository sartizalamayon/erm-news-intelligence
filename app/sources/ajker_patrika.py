"""Ajker Patrika — Bengali general daily. #33 by article volume.
News-sitemap, with a genuine empty-result RSS fallback (speed-
techniques.md §4 point 4). Config ported from
`backend/collector_logic.py` SOURCES list (:158-161).

Uses `require_news_sitemap` (not the bare `fetch_news_sitemap`) for the
primary, so "the sitemap is genuinely down" is distinguishable from
"the sitemap is fine, just empty today" — the RSS fallback is tried
either way, but only a confirmed-dead sitemap PLUS an empty/unreachable
RSS fallback raises. Without this, a totally dead host would report
`status="empty"`, identical to a healthy quiet day (docs/red-team-
findings.md RT-2 — this source was one of the two RSS-fallback sources
named in that finding, since the same symptom survives the fallback hop
unless BOTH stages are checked for genuine unreachability).
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

SITEMAP_URL = "https://www.ajkerpatrika.com/sitemaps/news-sitemap.xml"
FALLBACK_RSS_URL = "https://www.ajkerpatrika.com/feed"


@register
class AjkerPatrikaSource(NewsSource):
    key = "ajker_patrika"
    display_name = "Ajker Patrika"

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

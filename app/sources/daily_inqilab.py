"""Daily Inqilab — Bengali conservative daily. #1 by article volume
(8.2% of the legacy corpus). Google News sitemap, standard format.
Config ported from `backend/collector_logic.py` SOURCES list (:269-271).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._sitemap import require_news_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITEMAP_URL = "https://dailyinqilab.com/news-sitemap.xml"


@register
class DailyInqilabSource(NewsSource):
    key = "daily_inqilab"
    display_name = "Daily Inqilab"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        # require_news_sitemap (not the bare fetch_news_sitemap) so a
        # totally dead host raises SourceFetchError -> status="failed",
        # instead of looking identical to "healthy, no news today"
        # (docs/red-team-findings.md RT-2).
        return require_news_sitemap(self.session, SITEMAP_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

"""The Business Standard — English business daily. #10 by article
volume. News-sitemap. Config ported from `backend/collector_logic.py`
SOURCES list (:146-149) — legacy also set `force_gnews: True` (an
unconditional top-up union); not ported, per speed-techniques.md §4
point 4 (single authoritative method, fallback only on empty result).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._sitemap import require_news_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITEMAP_URL = "https://www.tbsnews.net/sitemap-news.xml"


@register
class BusinessStandardSource(NewsSource):
    key = "business_standard"
    display_name = "The Business Standard"

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

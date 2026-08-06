"""Daily Sun — English general daily. #13 by article volume. Same
Cloudflare-TLS workaround as Kalbela — fetched via system curl. Config
ported from `backend/collector_logic.py` SOURCES list (:398-399).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._curl_sitemap import fetch_curl_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITEMAP_URL = "https://www.daily-sun.com/news_sitemap.xml"


@register
class DailySunSource(NewsSource):
    key = "daily_sun"
    display_name = "Daily Sun"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_curl_sitemap(SITEMAP_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

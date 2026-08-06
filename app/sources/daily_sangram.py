"""Daily Sangram — Bengali general daily. #24 by article volume. Sitemap
index lists one dated file per day (`/sitemap/YYYY-MM-DD.xml`), keyed by
publish date. Config ported from `backend/collector_logic.py` SOURCES
list (:403-405).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._dated_sitemap import fetch_dated_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

URL_TEMPLATE = "https://dailysangram.com/sitemap/{date}.xml"


@register
class DailySangramSource(NewsSource):
    key = "daily_sangram"
    display_name = "Daily Sangram"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_dated_sitemap(self.session, URL_TEMPLATE, source=self.key, date_format="%Y-%m-%d")

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

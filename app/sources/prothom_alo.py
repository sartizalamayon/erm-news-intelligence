"""Prothom Alo — Bengali general daily, the largest Bengali outlet.
#3 by article volume. Quintype's internal `advanced-search` API is
strictly better than the news-sitemap: reliable UTC epoch timestamps,
offset pagination up to 20 pages/200 per page. Config ported from
`backend/collector_logic.py` SOURCES list (:119-130).
"""

from __future__ import annotations

from app.sources._api import ApiConfig, fetch_json_api
from app.sources._body import fetch_generic_article_body
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

CONFIG = ApiConfig(
    url=(
        "https://www.prothomalo.com/api/v1/advanced-search"
        "?sort=latest-published&limit=200&fields=headline,slug,url,published-at,first-published-at"
    ),
    items_path="items",
    title_field="headline",
    url_field="url",
    date_field="published-at",
    base_url="https://www.prothomalo.com",
    paginate="offset",
    offset_step=200,
    max_pages=20,
)


@register
class ProthomAloSource(NewsSource):
    key = "prothom_alo"
    display_name = "Prothom Alo"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_json_api(self.session, CONFIG, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

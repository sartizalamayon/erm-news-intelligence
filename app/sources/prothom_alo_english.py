"""Prothom Alo English — English edition of Prothom Alo. #31 by article
volume. Same Quintype `advanced-search` API family as `prothom_alo.py`,
different host. Config ported from `backend/collector_logic.py` SOURCES
list (:133-144).
"""

from __future__ import annotations

from app.sources._api import ApiConfig, fetch_json_api
from app.sources._body import fetch_generic_article_body
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

CONFIG = ApiConfig(
    url=(
        "https://en.prothomalo.com/api/v1/advanced-search"
        "?sort=latest-published&limit=200&fields=headline,slug,url,published-at,first-published-at"
    ),
    items_path="items",
    title_field="headline",
    url_field="url",
    date_field="published-at",
    base_url="https://en.prothomalo.com",
    paginate="offset",
    offset_step=200,
    max_pages=20,
)


@register
class ProthomAloEnglishSource(NewsSource):
    key = "prothom_alo_english"
    display_name = "Prothom Alo English"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_json_api(self.session, CONFIG, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

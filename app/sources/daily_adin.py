"""Daily Adin — Bengali general daily. #17 by article volume. Next.js
site, no feed, no Google News presence. Articles reachable via a
`/news/{id}` shell whose canonical URL carries the real JSON-LD
`datePublished` (UTC). Config ported from `backend/collector_logic.py`
SOURCES list (:374-381).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._id_enum import IdEnumConfig, fetch_id_enum
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

CONFIG = IdEnumConfig(
    site_url="https://dailyadin.com",
    article_pattern="/news/{id}",
    id_regex=r"/(\d{4,})(?:[\"/?#]|$)",
    canonical_hop=True,
    not_article_check="none",
)


@register
class DailyAdinSource(NewsSource):
    key = "daily_adin"
    display_name = "Daily Adin"

    def __init__(self) -> None:
        self.session = make_session(pool_size=CONFIG.workers)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_id_enum(self.session, CONFIG, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

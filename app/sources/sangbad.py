"""Sangbad — Bengali general daily. #18 by article volume. RSS is stale;
articles live at sequential `/news/{id}` URLs. JSON-LD `datePublished`
is Dhaka wall clock written with a bogus `+00:00` offset (hence
`date_tz_override="dhaka"`). Config ported from
`backend/collector_logic.py` SOURCES list (:182-191). Legacy's
`fallback_rss` is wired here as a genuine empty-result fallback
(speed-techniques.md §4 point 4) rather than dropped outright.
"""

from __future__ import annotations

from app.logging import get_logger
from app.sources._body import fetch_generic_article_body
from app.sources._id_enum import IdEnumConfig, fetch_id_enum
from app.sources._rss import fetch_rss
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

logger = get_logger(__name__)

CONFIG = IdEnumConfig(
    site_url="https://sangbad.net",
    article_pattern="/news/{id}",
    id_regex=r"/news/(\d+)",
    date_tz_override="dhaka",
    not_article_check="canonical_is_homepage",
)
FALLBACK_RSS_URL = "https://sangbad.net/rss.xml"


@register
class SangbadSource(NewsSource):
    key = "sangbad"
    display_name = "Sangbad"

    def __init__(self) -> None:
        self.session = make_session(pool_size=CONFIG.workers)

    def iter_feed_items(self) -> list[FeedItem]:
        items = fetch_id_enum(self.session, CONFIG, source=self.key)
        if items:
            return items
        logger.warning(
            "source_fallback_triggered",
            extra={"source": self.key, "primary_method": "id_enum", "fallback_method": "rss"},
        )
        return fetch_rss(self.session, FALLBACK_RSS_URL, source=self.key, required=False)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

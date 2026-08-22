"""Somoy Journal — Bengali general daily. #26 by article volume.
Section-agnostic `/news-post/{id}/-` (redirects to the slugged URL).
Date is Bengali text in `div.mb-3` — no JSON-LD. Config ported from
`backend/collector_logic.py` SOURCES list (:384-390).

Falls back to Google News search (`app.sources._fallback`) when the
id_enum homepage crawl fails. Measured live 2026-08-18: a lone request
completes in ~17s, comfortably inside the 30s read budget — this host
is genuinely alive, just marginal, and a real failure here is more
likely a slow day under this run's concurrent load than an outage. See
`_fallback.py`'s docstring for why this is a fallback rather than a
permanent method swap like Daily Sun/Kalbela.
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._fallback import fetch_with_gnews_fallback
from app.sources._id_enum import IdEnumConfig, fetch_id_enum
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

CONFIG = IdEnumConfig(
    site_url="https://somoyjournal.com",
    article_pattern="/news-post/{id}/-",
    id_regex=r"/news-post/(\d+)",
    date_source="bengali_date",
    not_article_check="none",
)
DOMAIN = "somoyjournal.com"


@register
class SomoyJournalSource(NewsSource):
    key = "somoy_journal"
    display_name = "Somoy Journal"

    def __init__(self) -> None:
        self.session = make_session(pool_size=CONFIG.workers)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_with_gnews_fallback(
            lambda: fetch_id_enum(self.session, CONFIG, source=self.key), self.session, DOMAIN, source=self.key
        )

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

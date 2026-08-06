"""The Financial Express — English business daily. #19 by article
volume. Cursor-paginated JSON API. Config confirmed live via
`sqlite3 -readonly data/news.db "SELECT config FROM sources WHERE
name='The Financial Express'"` (2026-08-06) — this source's config was
added by hand via the admin UI after `collector_logic.py`'s hardcoded
SOURCES list was written, so it isn't in that file's literal (see
legacy-behavior.md §2.1: the DB, not the code list, is the runtime
source of truth for DB-only additions).
"""

from __future__ import annotations

from app.sources._api import ApiConfig, fetch_json_api
from app.sources._body import fetch_generic_article_body
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

CONFIG = ApiConfig(
    url="https://api.thefinancialexpress.com.bd/api/en/posts",
    items_path="items",
    title_field="title",
    url_field="slug",
    date_field="datetime",
    base_url="https://thefinancialexpress.com.bd",
    next_field="nextPageUrl",
    max_pages=25,
    paginate="cursor",
    headers={
        "Origin": "https://thefinancialexpress.com.bd",
        "Referer": "https://thefinancialexpress.com.bd/",
        "X-Requested-With": "XMLHttpRequest",
    },
)


@register
class FinancialExpressSource(NewsSource):
    key = "financial_express"
    display_name = "The Financial Express"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_json_api(self.session, CONFIG, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

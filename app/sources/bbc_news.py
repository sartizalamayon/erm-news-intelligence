"""BBC News — English international news. #5 by article volume.
Sitemap-index scoped to `/news/` paths only. Config ported from
`backend/collector_logic.py` SOURCES list (:224-226).
"""

from __future__ import annotations

from app.sources._bbc import fetch_bbc_sitemaps
from app.sources._body import fetch_generic_article_body
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

INDEX_URL = "https://www.bbc.com/sitemaps/https-index-com-news.xml"


@register
class BbcNewsSource(NewsSource):
    key = "bbc_news"
    display_name = "BBC News"

    def __init__(self) -> None:
        # BBC/CNN were tuned DOWN from 8->4 workers in Repo B after
        # observing self-inflicted timeouts under load
        # (speed-techniques.md §2's "Concurrency model" section) — this
        # source's own discovery is a handful of sequential sitemap
        # fetches (no internal fan-out), so the smaller pool here is
        # about being gentle with a host known to be sensitive, not
        # about matching a worker count.
        self.session = make_session(pool_size=4)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_bbc_sitemaps(self.session, INDEX_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

"""Share Business 24 — Bangladeshi business and capital-markets daily.

Same CMS and same situation as Share Biz: the archive endpoint exists
but rejects the visible form's parameters with an "Undefined array key
action" PHP warning, so Google News is the primary method. 33 dated
items when measured.

See `app/sources/share_biz.py` for the full finding.
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

GNEWS_DOMAIN = "sharebusiness24.com"


@register
class ShareBusiness24Source(NewsSource):
    key = "share_business_24"
    display_name = "ShareBusiness24"

    def __init__(self) -> None:
        self.session = make_session(pool_size=6)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_gnews(self.session, GNEWS_DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

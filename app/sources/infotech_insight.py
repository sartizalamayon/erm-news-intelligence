"""InfoTech Insight — Bangladeshi technology and telecom coverage.

Directly relevant to this desk: a telecom-sector outlet is exactly the
kind of source a Grameenphone risk team wants read.

Google News is the primary method. The site DOES expose JSON-LD
`datePublished` on article pages, so `_id_enum`'s existing `jsonld`
variant could walk `/news/{id}` natively and would return more than the
7 dated items Google indexes. That is a real upgrade and it is written
down here rather than done, because an id-walk costs one request per
article against a small site and this source is worth a fraction of a
percent of the corpus. Revisit if its volume ever justifies the load.
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

GNEWS_DOMAIN = "infotechinsight.com"


@register
class InfoTechInsightSource(NewsSource):
    key = "infotech_insight"
    display_name = "InfoTech"

    def __init__(self) -> None:
        self.session = make_session(pool_size=6)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_gnews(self.session, GNEWS_DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

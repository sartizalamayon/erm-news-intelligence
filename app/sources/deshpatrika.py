"""Deshpatrika — Bengali general daily.

Google News is the PRIMARY method, for the same reason as BSS: this site
publishes no machine-readable publication date. Checked live 2026-08-17
on `details.php?news_id=14473`:

    JSON-LD datePublished        absent
    article:published_time       absent
    <time datetime=...>          absent

The one ISO date in the page (`2026-08-17`) is the archive form's
default value, i.e. TODAY, not the article's date. A parser keying on it
would stamp every article, however old, with the date it was crawled —
which is precisely the class of bug migrations/0018 exists to undo.

`app/sources/_id_enum.py`'s own docstring already records the earlier
decision not to port a bespoke date parser for this source. That
decision stands; this file simply gives the source a working method
instead of leaving it enabled and silently skipped.

Coverage is thin (4 dated items when measured) but honest.
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

GNEWS_DOMAIN = "deshpatrika24.com"


@register
class DeshpatrikaSource(NewsSource):
    key = "deshpatrika"
    display_name = "Deshpatrika"

    def __init__(self) -> None:
        self.session = make_session(pool_size=6)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_gnews(self.session, GNEWS_DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

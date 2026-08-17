"""Share Biz — Bangladeshi business and capital-markets daily.

Google News is the primary method. The site has an archive endpoint
(`/ajax_archive_load_more.php`) that its own "load more" button calls,
but posting to it returns a PHP warning rather than a fragment:

    Warning: Undefined array key "action" in
    /home/share5biz632/public_html/ajax_archive_load_more.php on line 2

so it expects at least one more parameter than the archive page's
visible form supplies. Reverse-engineering that is possible, but it
would produce a scraper keyed to an undocumented private parameter on a
site that returns 100 dated items through Google News today.

The `_archive_listing` family is already built and wired for exactly
this endpoint shape, so switching over later is a config change, not a
rewrite.
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

GNEWS_DOMAIN = "sharebiz.net"


@register
class ShareBizSource(NewsSource):
    key = "share_biz"
    display_name = "ShareBiz"

    def __init__(self) -> None:
        self.session = make_session(pool_size=6)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_gnews(self.session, GNEWS_DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

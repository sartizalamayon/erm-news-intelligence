"""Channel i Online — Bengali general daily. #27 by article volume.
WordPress REST API is open. Config ported from
`backend/collector_logic.py` SOURCES list (:316-317).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._wp_rest import fetch_wp_rest
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITE_URL = "https://www.channelionline.com"


@register
class ChannelIOnlineSource(NewsSource):
    key = "channel_i_online"
    display_name = "Channel i Online"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_wp_rest(self.session, SITE_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

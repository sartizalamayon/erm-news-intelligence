"""Daily Star — English general daily. #9 by article volume. Drupal
site with no usable feed (sitemap is 83 pages of priority-ordered
chaos, RSS caps at 10/section); descending `/node/{id}` walk is
complete by construction. Config ported from
`backend/collector_logic.py` SOURCES list (:412-416).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._daily_star import fetch_daily_star_nodes
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITE_URL = "https://www.thedailystar.net"


@register
class DailyStarSource(NewsSource):
    key = "daily_star"
    display_name = "Daily Star"

    def __init__(self) -> None:
        self.session = make_session(pool_size=12)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_daily_star_nodes(self.session, SITE_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

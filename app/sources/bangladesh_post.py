"""Bangladesh Post — English general daily. #30 by article volume.
WordPress REST API is open. Legacy also configured `fallback_gnews`;
wired here as a genuine empty-result fallback (speed-techniques.md §4
point 4), not an always-on union. Config ported from
`backend/collector_logic.py` SOURCES list (:313-314).

The gnews fallback fires on BOTH an empty wp_rest result AND a raised
`SourceFetchError` (e.g. a definitive 404/403 on the WP REST endpoint —
observed live during this task's GATE 2 measurement run, see the task
report). Originally this only checked `if items:`, so a hard error from
`fetch_wp_rest` propagated straight out of `iter_feed_items()` and never
reached the fallback at all — the exact case a live run hit for real.
"""

from __future__ import annotations

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources._wp_rest import fetch_wp_rest
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

logger = get_logger(__name__)

SITE_URL = "https://bangladeshpost.net"
FALLBACK_GNEWS_DOMAIN = "bangladeshpost.net"


@register
class BangladeshPostSource(NewsSource):
    key = "bangladesh_post"
    display_name = "Bangladesh Post"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        try:
            items = fetch_wp_rest(self.session, SITE_URL, source=self.key)
        except SourceFetchError as exc:
            logger.warning(
                "source_fallback_triggered",
                extra={
                    "source": self.key,
                    "primary_method": "wp_rest",
                    "fallback_method": "gnews",
                    "primary_error": str(exc),
                },
            )
            return fetch_gnews(self.session, FALLBACK_GNEWS_DOMAIN, source=self.key)

        if items:
            return items

        logger.warning(
            "source_fallback_triggered",
            extra={"source": self.key, "primary_method": "wp_rest", "fallback_method": "gnews"},
        )
        return fetch_gnews(self.session, FALLBACK_GNEWS_DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

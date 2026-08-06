"""Al Jazeera — English international news. #21 by article volume
(bonus source, pushes coverage from 80.3% to 82.0% — see the task
report). Per-date sitemaps at `/sitemaps/article-new/DD-MM-YYYY.xml`
(UTC calendar day) are each a COMPLETE set for that day. Config ported
from `backend/collector_logic.py` SOURCES list (:166-169) /
`iter_aljazeera_dated` (:823-831).

Not built on `app.sources._dated_sitemap` because that helper computes
"today"/"yesterday" in Dhaka local time; Al Jazeera's dated files are
keyed by UTC calendar day (legacy's own `NOW.astimezone(timezone.utc)`),
a genuinely different date boundary worth keeping distinct rather than
overloading one helper with a timezone parameter for a single caller.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.errors import SourceFetchError
from app.sources._body import fetch_generic_article_body
from app.sources._sitemap import fetch_one_sitemap_url
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import get, make_session
from app.sources.registry import register

BASE_URL = "https://www.aljazeera.com/sitemaps/article-new/"
DAYS_BACK = 2
CAP_PER_DAY = 300


@register
class AlJazeeraSource(NewsSource):
    key = "al_jazeera"
    display_name = "Al Jazeera"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        now_utc = datetime.now(timezone.utc)
        items: list[FeedItem] = []
        seen: set[str] = set()
        for days in range(DAYS_BACK):
            d = now_utc - timedelta(days=days)
            url = f"{BASE_URL}{d.strftime('%d-%m-%Y')}.xml"
            day_items = fetch_one_sitemap_url(self.session, url, source=self.key, cap=CAP_PER_DAY)
            if not day_items and days == 0:
                probe = get(self.session, url, source=self.key, timeout=10, max_retries=1)
                if probe is None:
                    raise SourceFetchError(f"al_jazeera dated sitemap unreachable: {url}", source=self.key)
            for item in day_items:
                if item.url not in seen:
                    seen.add(item.url)
                    items.append(item)
        return items

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

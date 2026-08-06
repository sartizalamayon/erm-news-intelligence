"""Per-date sitemap files (`{date}`-templated URL) — Naya Diganta
(#2 by volume) and, outside the ported set, Daily Sangram/Bonik Barta.

Ported from `collector_logic.iter_dated_sitemap` (:834-845): fetches
today's and yesterday's dated file (Dhaka-local date), each a complete
article set for that day.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import requests

from app.errors import SourceFetchError
from app.sources._sitemap import fetch_one_sitemap_url
from app.sources._util import DHAKA
from app.sources.base import FeedItem
from app.sources.http import get


def fetch_dated_sitemap(
    session: requests.Session,
    url_template: str,
    *,
    source: str,
    date_format: str = "%Y-%m-%d",
    days_back: int = 2,
    cap: int = 500,
) -> list[FeedItem]:
    """`url_template` uses `{date}` as a placeholder. Raises
    `SourceFetchError` only if TODAY's file is both empty AND
    unreachable (yesterday's is always best-effort — a source is not
    "down" just because the day-old file 404s).
    """
    now = datetime.now(DHAKA)
    items: list[FeedItem] = []
    seen_urls: set[str] = set()
    for days in range(days_back):
        d = now - timedelta(days=days)
        date_str = d.strftime(date_format)
        url = url_template.replace("{date}", date_str)
        day_items = fetch_one_sitemap_url(session, url, source=source, cap=cap)
        if not day_items and days == 0:
            probe = get(session, url, source=source, timeout=10, max_retries=1)
            if probe is None:
                raise SourceFetchError(f"dated sitemap unreachable: {url}", source=source)
        for item in day_items:
            if item.url not in seen_urls:
                seen_urls.add(item.url)
                items.append(item)
    return items

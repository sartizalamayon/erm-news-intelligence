"""BBC News' own sitemap-index scoping (#5 by volume). Ported from
`collector_logic.iter_bbc_sitemaps` (:931-958): follows the sitemap
index to its (currently 3) sub-sitemaps, scopes results to `/news/`
paths only (excluding `/sport/`, language editions, `/videos/`, `/av/`),
and returns newest-first.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import requests

from app.errors import SourceFetchError
from app.sources._sitemap import fetch_one_sitemap_url
from app.sources.base import FeedItem
from app.sources.http import get

_SUB_SITEMAP_RE = re.compile(r"<loc>(https://www\.bbc\.com/sitemaps/https-sitemap-com-news-\d+\.xml)</loc>")
_NEWS_PATH_RE = re.compile(r"^/news/(?!av/|videos/)")


def fetch_bbc_sitemaps(
    session: requests.Session, index_url: str, *, source: str, cap_per_child: int = 500
) -> list[FeedItem]:
    response = get(session, index_url, source=source, timeout=30)
    if response is None:
        raise SourceFetchError(f"BBC sitemap index unreachable: {index_url}", source=source)
    if response.status_code != 200:
        return []
    sub_urls = _SUB_SITEMAP_RE.findall(response.text)
    if not sub_urls:
        return []

    seen: set[str] = set()
    items: list[FeedItem] = []
    for sub_url in sub_urls:
        for item in fetch_one_sitemap_url(session, sub_url, source=source, cap=cap_per_child, timeout=30):
            path = item.url.split("bbc.com", 1)[-1] if "bbc.com" in item.url else ""
            if not _NEWS_PATH_RE.match(path):
                continue
            if item.url in seen:
                continue
            seen.add(item.url)
            items.append(item)

    items.sort(key=lambda it: it.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items

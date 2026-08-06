"""Sitemap-index merge — Rupali BD (#14 by volume), whose single
news-sitemap misses sections; unioning all news/post/article/latest
-looking child sitemaps from the index gives complete coverage.

Ported from `collector_logic.iter_sitemap_index_merge` (:903-928).
"""

from __future__ import annotations

import requests

from app.errors import SourceFetchError
from app.sources._sitemap import fetch_one_sitemap_url
from app.sources._xml import local_tag, parse_xml
from app.sources.base import FeedItem
from app.sources.http import get

_NEWS_LIKE_KEYWORDS = ("news", "post", "article", "latest")


def fetch_sitemap_index_merge(
    session: requests.Session, index_url: str, *, source: str, cap_per_child: int = 200
) -> list[FeedItem]:
    response = get(session, index_url, source=source, timeout=30)
    if response is None:
        raise SourceFetchError(f"sitemap index unreachable: {index_url}", source=source)
    if response.status_code != 200:
        return []

    root = parse_xml(response.content)
    if root is None:
        return []
    if local_tag(root.tag) != "sitemapindex":
        # Not actually an index — treat as one regular sitemap.
        return fetch_one_sitemap_url(session, index_url, source=source, cap=600)

    seen: set[str] = set()
    items: list[FeedItem] = []
    for sm in root:
        if local_tag(sm.tag) != "sitemap":
            continue
        loc_el = next((c for c in sm if local_tag(c.tag) == "loc"), None)
        if loc_el is None or not loc_el.text:
            continue
        child_url = loc_el.text.strip()
        if not any(kw in child_url.lower() for kw in _NEWS_LIKE_KEYWORDS):
            continue
        for item in fetch_one_sitemap_url(session, child_url, source=source, cap=cap_per_child, timeout=15):
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
    return items

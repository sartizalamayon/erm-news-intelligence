"""RSS 2.0 / Atom feed parsing — NDTV's `rss_multi` (4 FeedBurner
mirrors merged, #16 by volume) and the shared parser Google News search
results (`_gnews.py`) also use, since that endpoint returns RSS 2.0.

Ported from `collector_logic.iter_rss`/`iter_rss_multi` (:738-762), but
without the `feedparser` dependency (not installed — see
`app/sources/_util.py`'s docstring): RSS/Atom is well-formed XML in
practice, so `xml.etree.ElementTree` parses it directly rather than
needing a permissive third-party feed parser.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime

import requests

from app.errors import SourceFetchError
from app.sources._util import clean_title, parse_dt
from app.sources._xml import local_tag, parse_xml
from app.sources.base import FeedItem
from app.sources.http import get


def _entries(root: ET.Element) -> list[ET.Element]:
    # RSS 2.0: <rss><channel><item>...  Atom: <feed><entry>...
    return [el for el in root.iter() if local_tag(el.tag) in ("item", "entry")]


def parse_feed_bytes(content: bytes) -> list[tuple[str, str, datetime | None]]:
    """Returns (title, link, dt) tuples. `dt` may be `None`."""
    root = parse_xml(content)
    if root is None:
        return []
    out: list[tuple[str, str, datetime | None]] = []
    for entry in _entries(root):
        link = None
        title_text = None
        date_text = None
        for child in entry:
            lt = local_tag(child.tag)
            if lt == "link" and link is None:
                # RSS: <link>text</link>. Atom: <link href="...">.
                link = (child.text or "").strip() or child.attrib.get("href", "").strip()
            elif lt == "title" and title_text is None:
                title_text = child.text
            elif lt in ("pubDate", "published", "updated") and date_text is None:
                date_text = child.text
        if not link:
            continue
        title = clean_title(title_text or "")
        dt = parse_dt(date_text) if date_text else None
        out.append((title, link, dt))
    return out


def fetch_rss(
    session: requests.Session,
    url: str,
    *,
    source: str,
    required: bool = True,
    max_retries: int = 3,
    timeout: float = 15.0,
) -> list[FeedItem]:
    response = get(session, url, source=source, timeout=timeout, max_retries=max_retries)
    if response is None:
        if required:
            raise SourceFetchError(f"rss feed unreachable: {url}", source=source)
        return []
    if response.status_code != 200:
        if required:
            raise SourceFetchError(f"rss feed returned HTTP {response.status_code}: {url}", source=source)
        return []
    tuples = parse_feed_bytes(response.content)
    return [FeedItem(url=u, title=t, source_key=source, published_at=dt) for (t, u, dt) in tuples if t]


def fetch_rss_multi(session: requests.Session, urls: list[str], *, source: str, max_retries: int = 3) -> list[FeedItem]:
    """Merge several section feeds, deduping by article URL — used for
    sites whose master feed is stale but section feeds are live.
    Raises only if EVERY feed in the list is unreachable.
    """
    seen: set[str] = set()
    items: list[FeedItem] = []
    any_ok = False
    for feed_url in urls:
        feed_items = fetch_rss(session, feed_url, source=source, required=False, max_retries=max_retries)
        if feed_items:
            any_ok = True
        for item in feed_items:
            if item.url not in seen:
                seen.add(item.url)
                items.append(item)
    if not any_ok and urls:
        raise SourceFetchError(f"all {len(urls)} rss_multi feeds unreachable", source=source)
    return items

"""Google News RSS search — BD Pratidin and New Age (#6 and #12 by
volume) use this as their SOLE authoritative method (their own
feed/sitemap is bot-blocked or absent; this is not a "top-up" the way
legacy's always-on gnews fallback was — speed-techniques.md §4 point 4).

Ported from `collector_logic.iter_gnews` (:804-818). Coverage is "a
curated subset, not 100%" (legacy's own documented caveat) — kept as
each source's ONE method because it's genuinely the best available
signal for these hosts, not a hedge on top of something else.

The `time.sleep(1.0)` throttle is preserved: when several gnews-method
sources run in the same batch, Google News rate-limits and returns
empty without it (legacy's own documented reason,
collector_logic.py:810-812).
"""

from __future__ import annotations

import time
from urllib.parse import quote

import requests

from app.sources._rss import fetch_rss
from app.sources.base import FeedItem

_GNEWS_THROTTLE_SECONDS = 1.0


def fetch_gnews(session: requests.Session, domain: str, *, source: str) -> list[FeedItem]:
    time.sleep(_GNEWS_THROTTLE_SECONDS)
    query = quote(f"site:{domain} when:2d")
    url = f"https://news.google.com/rss/search?q={query}&hl=en-BD&gl=BD&ceid=BD:en"
    return fetch_rss(session, url, source=source, required=True)

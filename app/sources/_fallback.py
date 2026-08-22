"""Shared fallback wrapper: try a source's own primary fetch method
first, and only reach for Google News search (`app.sources._gnews`) if
that raises `SourceFetchError`.

Distinct from the Daily Sun / Kalbela case (`app.sources.daily_sun`,
`app.sources.kalbela`): those hosts serve an active Cloudflare JS
challenge to every request, which no header/timeout tuning can beat, so
gnews REPLACED their primary method outright. Nagorik and Somoy Journal
are not like that — Nagorik's site is reachable most of the time and
Somoy Journal responds, just slowly (both measured live, 2026-08-18: a
lone request to Somoy Journal completed in ~17s, comfortably inside the
30s read budget — it was a real request that happened to time out under
this run's concurrent load, not a dead host). Their PRIMARY method is
still the better source (wp_rest's full pagination / id_enum's fuller
crawl beat gnews's narrower recent-item search) whenever the site
cooperates — this only steps in for the day the site doesn't, so a
transient outage or a slow run costs volume for that one source on that
one day, not a permanent downgrade like the Cloudflare pair.
"""

from __future__ import annotations

from collections.abc import Callable

import requests

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem

logger = get_logger(__name__)


def fetch_with_gnews_fallback(
    primary: Callable[[], list[FeedItem]], session: requests.Session, domain: str, *, source: str
) -> list[FeedItem]:
    """Call `primary()`; on `SourceFetchError`, fall back to
    `fetch_gnews(session, domain, source=source)` instead of letting the
    whole source fail for the day. If gnews ALSO raises, that exception
    propagates — a source that is unreachable by every method it has is
    correctly reported as failed, not silently swallowed into 0 results
    (speed-techniques.md §5's loud-fail guard).
    """
    try:
        return primary()
    except SourceFetchError as exc:
        logger.warning(
            "primary_fetch_failed_falling_back_to_gnews",
            extra={"source": source, "error": str(exc)[:300]},
        )
        return fetch_gnews(session, domain, source=source)

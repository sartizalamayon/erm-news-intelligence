"""Google News RSS search — the SOLE authoritative method for 9 sources
whose own feed/sitemap is bot-blocked or absent (economy_post,
techworld, digibangla, tech_shohor, bonik_barta, bangladesh_post,
bd_pratidin, new_age, news24_bd). This is not a "top-up" the way
legacy's always-on gnews fallback was (speed-techniques.md §4 point 4).

Ported from `collector_logic.iter_gnews` (:804-818).

THE LOCALE BUG (fixed 2026-08-14)
---------------------------------
The ported query hardcoded ONE locale — `hl=en-BD&ceid=BD:en` — and a
2-day window. Google News honours `hl`/`ceid` as a hard content filter,
not a display preference, so an English-locale query returns nothing at
all for a Bengali-language publisher. Every Bengali source using this
method had been silently returning zero articles, rendered in the run
panel as an unremarkable "0 fetched · 0 new" — indistinguishable from a
quiet news day.

Measured live, items returned per query (2026-08-14):

    domain                en/2d(old)   bn/2d   bn/7d   en/7d
    economipost.com                0      13      35       0
    techworldbd.com                0      16      44       0
    digibanglatech.news            0      53     100       0
    techshohor.com                 0       0       3       0
    news24bd.tv                    0     100     100       2
    newagebd.net                 100       0       0     100
    bangladeshpost.net            55       0       0     100
    bd-pratidin.com              100     100     100     100

Neither locale alone is correct: Bengali publishers need `bn`, English
ones (New Age, Bangladesh Post) need `en`, and which is which is a
property of the publication, not something worth maintaining as
per-source config that can drift out of date. Both are queried and the
results merged, so a source is covered whichever language it publishes
in — including outlets that publish in both.

`bonikbarta.net` returns 0 under every variant; it is genuinely absent
from Google News' index and needs a different method entirely. That is
a real gap, and it is not hidden: with `required=True` semantics below,
a source whose every locale comes back empty raises rather than
reporting a successful empty fetch.
"""

from __future__ import annotations

import time
from urllib.parse import quote

import requests

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._rss import fetch_rss
from app.sources.base import FeedItem

logger = get_logger(__name__)

_GNEWS_THROTTLE_SECONDS = 1.0

# Widened from `when:2d`. The 2-day window was costing real recall on
# lower-volume publishers (economipost 13 -> 35, techworldbd 16 -> 44)
# for no benefit: re-seeing an article we already hold is free, because
# `collector.write_items` rejects it on the `url_norm` unique index
# before any work happens.
_GNEWS_WINDOW = "when:7d"

# (hl, ceid) pairs. Bengali first — most of this registry is Bengali, so
# the more productive query runs before the throttle on the second.
_GNEWS_LOCALES: tuple[tuple[str, str], ...] = (("bn", "BD:bn"), ("en-BD", "BD:en"))


def _search_one(session: requests.Session, domain: str, *, source: str, hl: str, ceid: str) -> list[FeedItem]:
    query = quote(f"site:{domain} {_GNEWS_WINDOW}")
    url = f"https://news.google.com/rss/search?q={query}&hl={hl}&gl=BD&ceid={ceid}"
    # required=False per locale: one locale legitimately returning
    # nothing is the NORMAL case (an English-language paper has no
    # Bengali results), not a failure. Whether the source as a whole
    # failed is decided by the caller, after all locales are tried.
    return fetch_rss(session, url, source=source, required=False)


def fetch_gnews(session: requests.Session, domain: str, *, source: str) -> list[FeedItem]:
    """Search Google News for `domain` across every supported locale and
    return the merged, de-duplicated result.

    Raises `SourceFetchError` when EVERY locale came back empty — the
    "loud-fail guard" (speed-techniques.md §5): a source that is blocked
    or de-indexed must not be reportable as a successful run that just
    happened to find nothing.
    """
    merged: list[FeedItem] = []
    seen: set[str] = set()
    per_locale: dict[str, int] = {}

    for hl, ceid in _GNEWS_LOCALES:
        # Throttle BEFORE each call: several gnews sources run in the
        # same batch and Google rate-limits to empty without it
        # (legacy's own documented reason, collector_logic.py:810-812).
        time.sleep(_GNEWS_THROTTLE_SECONDS)
        items = _search_one(session, domain, source=source, hl=hl, ceid=ceid)
        per_locale[hl] = len(items)
        for item in items:
            if item.url in seen:
                continue
            seen.add(item.url)
            merged.append(item)

    if not merged:
        logger.warning("gnews_all_locales_empty", extra={"source": source, "domain": domain})
        raise SourceFetchError(
            f"Google News returned no results for site:{domain} in any locale "
            f"({', '.join(hl for hl, _ in _GNEWS_LOCALES)})",
            source=source,
        )

    logger.info(
        "gnews_merged",
        extra={"source": source, "domain": domain, "total": len(merged), "per_locale": per_locale},
    )
    return merged

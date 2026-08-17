"""The site's own archive, driven through the endpoint its "load more"
button uses — the authoritative method for the three sources that have
no feed, no sitemap and no API: Amar Barta, Share Biz and Share
Business 24.

All three were seeded in the `sources` table with `method` set
(`amarbarta_archive`, `ajax_archive`, `ajax_archive`) but no plugin
behind it, so `registry_key` stayed NULL and collection — which
iterates the CODE registry and uses the table only to veto — skipped
them on every run since the rebuild began.

Why this method and not something cheaper (checked live 2026-08-17):

    amarbarta.com      /sitemap.xml, /feed, /rss, /wp-json  ->  soft-404
                       (an HTML catch-all page, HTTP 200)
    sharebiz.net       same; no news sitemap at any known path
    sharebusiness24.com same

What all three DO expose is a paginated archive fragment containing
everything a FeedItem needs — article URL, headline and a publication
timestamp — so discovery costs one request per 20 articles and never
touches an article page. That is strictly better than the id-walk
alternative, which would cost one request per article.

Two pagination shapes, because the two CMSes differ:

  * OFFSET (`fetch_offset_archive`) — Share Biz / Share Business 24, on
    the eMythMakers CMS. One global newest-first list; `newsCount` is a
    row offset. Walk from 0 until the items fall out of the rolling
    window. The POST body is the site's own default parameter set,
    copied verbatim from the archive page's inline script, including
    its literal `q` ordering clause; nothing in it is interpolated from
    anything we compute, so this sends the endpoint exactly what its
    own "load more" button sends.

  * DATED (`fetch_dated_archive`) — Amar Barta. The archive is
    addressed by day (`?page=N&date=YYYY-MM-DD`), so this walks today
    and the previous `days_back - 1` Dhaka days, paginating each to
    exhaustion.

Both share `parse_listing`, which segments a fragment on a per-site
marker and pulls (url, title, published_at) out of each segment.
Segmenting first — rather than running one big regex across the whole
fragment — is deliberate: a single item missing its date would
otherwise let a non-greedy match run on into the NEXT item's date and
silently misdate everything below it.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import requests

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._util import BN_MONTH_ALT, BN_MONTHS, DHAKA, bn_digits, clean_title, is_recent
from app.sources.base import FeedItem
from app.sources.http import get, get_or_raise

logger = get_logger(__name__)

# Per-source ceilings. A listing page is ~20 articles, so the page caps
# below bound discovery at a few hundred articles — comfortably more
# than any of these three outlets publishes in the rolling window, and
# small enough that a site that starts returning an endless identical
# page cannot hang the run.
MAX_PAGES = 20
# Consecutive items already outside the rolling window that end an
# offset walk. More than one, because a single out-of-order republished
# story must not truncate the walk.
STOP_AFTER_OLD_ITEMS = 8
PAGE_THROTTLE_SECONDS = 0.2

# `০৭:০৬ পিএম, ১৬ আগস্ট ২০২৬ রোববার` -> 2026-08-16 19:06 Dhaka.
# Digits are transliterated before matching, so only the month name and
# the meridiem stay in Bengali script.
_BN_AMPM_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(এএম|পিএম)[,\s]+(\d{1,2})\s+(" + BN_MONTH_ALT + r")\s+(\d{4})")
# `২০২৬-০৮-১৬ ২৩:৩২:৪৩ ইং` -> 2026-08-16 23:32:43 Dhaka.
_BN_ISO_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?")


def parse_bn_ampm(text: str) -> datetime | None:
    """Bengali 12-hour clock + Bengali month name, Dhaka wall clock."""
    m = _BN_AMPM_RE.search(bn_digits(text))
    if not m:
        return None
    hour, minute, meridiem, day, month_name, year = m.groups()
    month = BN_MONTHS.get(month_name)
    if month is None:
        return None
    h = int(hour) % 12
    if meridiem == "পিএম":
        h += 12
    try:
        return datetime(int(year), month, int(day), h, int(minute), tzinfo=DHAKA)
    except ValueError:
        return None


def parse_bn_iso(text: str) -> datetime | None:
    """Bengali-numeral `YYYY-MM-DD HH:MM[:SS]`, Dhaka wall clock."""
    m = _BN_ISO_RE.search(bn_digits(text))
    if not m:
        return None
    year, month, day, hour, minute, second = m.groups()
    try:
        return datetime(int(year), int(month), int(day), int(hour), int(minute), int(second or 0), tzinfo=DHAKA)
    except ValueError:
        return None


_DATE_PARSERS = {"bn_ampm": parse_bn_ampm, "bn_iso": parse_bn_iso}


@dataclass
class ArchiveListingConfig:
    """How to read ONE site's archive fragment, plus how to page it."""

    site_url: str
    endpoint: str
    # Marks the start of one article block inside a fragment. Must occur
    # exactly once per article.
    item_marker: str
    # Applied WITHIN one block: the article URL, the headline, the
    # publication timestamp. First match wins.
    link_regex: str
    title_regex: str
    date_regex: str
    date_format: str  # a key of _DATE_PARSERS
    # Offset pagination (fetch_offset_archive).
    form_data: dict[str, str] = field(default_factory=dict)
    offset_field: str = "newsCount"
    page_size: int = 20
    max_pages: int = MAX_PAGES
    # Dated pagination (fetch_dated_archive).
    page_param: str = "page"
    date_param: str = "date"
    days_back: int = 2
    first_page: int = 0
    # eMythMakers' archive query is genuinely slow (~25s/page measured
    # on sharebusiness24.com), which is past `http.READ_TIMEOUT`.
    timeout: float | tuple[float, float] | None = None


def parse_listing(fragment: str, cfg: ArchiveListingConfig, *, source: str) -> list[FeedItem]:
    """One archive fragment -> FeedItems, in the order the site listed
    them. Items with no parseable URL are skipped; items with no
    parseable date keep `published_at=None` rather than being dropped,
    so a template tweak degrades to "undated" instead of "this source
    is empty".
    """
    marker = re.compile(cfg.item_marker)
    link_re = re.compile(cfg.link_regex)
    title_re = re.compile(cfg.title_regex, re.S)
    date_re = re.compile(cfg.date_regex, re.S)
    parse_date = _DATE_PARSERS[cfg.date_format]

    starts = [m.start() for m in marker.finditer(fragment)]
    if not starts:
        return []
    bounds = list(zip(starts, starts[1:] + [len(fragment)]))

    items: list[FeedItem] = []
    seen: set[str] = set()
    for start, end in bounds:
        block = fragment[start:end]
        link_match = link_re.search(block)
        if not link_match:
            continue
        url = link_match.group("url") if "url" in link_re.groupindex else link_match.group(1)
        if not url.startswith("http"):
            url = cfg.site_url.rstrip("/") + "/" + url.lstrip("/")
        if url in seen:
            continue
        seen.add(url)
        title_match = title_re.search(block)
        title = clean_title(title_match.group(1)) if title_match else ""
        date_match = date_re.search(block)
        published = parse_date(date_match.group(1)) if date_match else None
        items.append(
            FeedItem(
                url=url,
                title=title,
                source_key=source,
                published_at=published,
                source_id=_id_from_url(url),
            )
        )
    return items


_TRAILING_ID_RE = re.compile(r"/(\d+)/?$")


def _id_from_url(url: str) -> str | None:
    m = _TRAILING_ID_RE.search(url)
    return m.group(1) if m else None


def _recent_only(items: list[FeedItem]) -> list[FeedItem]:
    """Same three-valued window filter every other family applies:
    keep recent and undated, drop confidently-old."""
    return [i for i in items if is_recent(i.published_at) is not False]


def fetch_offset_archive(session: requests.Session, cfg: ArchiveListingConfig, *, source: str) -> list[FeedItem]:
    """Walk a single newest-first archive list by row offset until the
    rolling window is exhausted.

    Raises `SourceFetchError` if the FIRST page cannot be read or yields
    no parseable items at all — that is the endpoint being broken or
    moved, and it must not be reported as a quiet news day. A later page
    failing simply ends the walk with what has already been collected.
    """
    kwargs: dict[str, Any] = {"timeout": cfg.timeout} if cfg.timeout else {}
    collected: list[FeedItem] = []
    seen: set[str] = set()
    old_seen = 0

    for page in range(cfg.max_pages):
        data = dict(cfg.form_data)
        data[cfg.offset_field] = str(page * cfg.page_size)
        response = get(session, cfg.endpoint, source=source, method="POST", data=data, **kwargs)
        if response is None or response.status_code != 200:
            if page == 0:
                raise SourceFetchError(
                    f"archive endpoint unreachable: {cfg.endpoint} "
                    f"(HTTP {response.status_code if response else 'no response'})",
                    source=source,
                )
            break

        items = parse_listing(response.text, cfg, source=source)
        if not items:
            if page == 0:
                raise SourceFetchError(
                    f"archive endpoint returned no parseable articles: {cfg.endpoint}",
                    source=source,
                )
            break

        fresh = 0
        for item in items:
            if item.url in seen:
                continue
            seen.add(item.url)
            fresh += 1
            if is_recent(item.published_at) is False:
                old_seen += 1
            else:
                collected.append(item)
        if fresh == 0 or old_seen >= STOP_AFTER_OLD_ITEMS:
            break
        time.sleep(PAGE_THROTTLE_SECONDS)

    return collected


def fetch_dated_archive(session: requests.Session, cfg: ArchiveListingConfig, *, source: str) -> list[FeedItem]:
    """Walk a day-addressed archive: today, then back `days_back - 1`
    Dhaka days, paginating each day to exhaustion.

    Raises `SourceFetchError` only when the endpoint itself is
    unreachable on the first request. An empty page-0 for TODAY is a
    normal early-morning result on a day-addressed archive, not a
    failure — which is exactly why the walk covers yesterday too.
    """
    kwargs: dict[str, Any] = {"timeout": cfg.timeout} if cfg.timeout else {}
    now = datetime.now(DHAKA)
    collected: list[FeedItem] = []
    seen: set[str] = set()

    for days in range(cfg.days_back):
        day = (now - timedelta(days=days)).date().isoformat()
        for page in range(cfg.first_page, cfg.first_page + cfg.max_pages):
            params = {cfg.page_param: str(page), cfg.date_param: day}
            if days == 0 and page == cfg.first_page:
                response = get_or_raise(session, cfg.endpoint, source=source, params=params, **kwargs)
            else:
                probe = get(session, cfg.endpoint, source=source, params=params, **kwargs)
                if probe is None or probe.status_code != 200:
                    break
                response = probe
            if response.status_code != 200:
                break

            items = parse_listing(response.text, cfg, source=source)
            fresh = [i for i in items if i.url not in seen]
            if not fresh:
                break
            for item in fresh:
                seen.add(item.url)
            collected.extend(fresh)
            time.sleep(PAGE_THROTTLE_SECONDS)

    kept = _recent_only(collected)
    if collected and not kept:
        logger.info(
            "archive_listing_all_items_outside_window",
            extra={"source": source, "discovered": len(collected)},
        )
    return kept

"""Amar Barta — Bengali general daily.

Its archive endpoint is the discovery method, because nothing cheaper
exists. Checked live 2026-08-17 against www.amarbarta.com:

    /sitemap.xml, /feed, /rss, /wp-json   ->  soft 404
                                              (HTML catch-all, HTTP 200)

What it DOES expose is the fragment its own archive "load more" button
requests, and that fragment carries everything a FeedItem needs:

    <a href="https://www.amarbarta.com/national/83670">
    ...
    <div style="color: #666">২০২৬-০৮-১৭ ১৪:১২:১৫

so one request yields a page of articles with real publication
timestamps, and no article page is touched during discovery.

The timestamp is ISO order (YYYY-MM-DD HH:MM:SS) written in Bengali
digits, in Dhaka local time. `_util.bn_digits` handles the numerals; the
shape is otherwise ordinary.

Google News is wired as a fallback rather than the primary. It returns
about 100 dated items for this domain, which is real coverage, but the
archive returns the paper's own listing rather than whatever Google
happened to index, so the archive leads and gnews catches a template
change or an outage.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources._util import DHAKA, bn_digits, clean_title, is_recent
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import get_or_raise, make_session
from app.sources.registry import register

logger = get_logger(__name__)

SITE_URL = "https://www.amarbarta.com"
ARCHIVE_PATH = "/templates/web-view/archive_page/more_archive_ajax.php"
FALLBACK_GNEWS_DOMAIN = "amarbarta.com"

# How many archive pages to walk. Each yields roughly a dozen articles,
# and `is_recent` stops the walk as soon as a page falls out of the
# rolling window, so this is only the ceiling for a quiet day.
MAX_PAGES = 12

# Segment on the CARD CONTAINER, not on anchors.
#
# Each article contributes TWO anchors to the fragment (one wrapping its
# thumbnail, one wrapping its headline) with the section link and the
# timestamp in between. Bounding a block on "the next anchor" therefore
# cut every card in half and lost the headline, which sits in the second
# anchor: the first version of this parser returned 20 correctly dated
# items with 20 empty titles.
#
# Segmenting on the container first, then searching within it, is the
# same shape `_archive_listing.parse_listing` uses and for the same
# reason: a field missing from one card must not let a regex run on into
# the next card and attribute its data to the wrong article.
_CARD_RE = re.compile(r'<div class="col-12 col-md-3')
_ANCHOR_RE = re.compile(r'href="((?:https?://[^"]*?)?/[a-z0-9-]+/(\d+))"', re.I)
_DATE_RE = re.compile(r"([০-৯]{4}-[০-৯]{2}-[০-৯]{2}\s+[০-৯]{2}:[০-৯]{2}:[০-৯]{2})")
_TITLE_RE = re.compile(r'<h5[^>]*class="title"[^>]*>(.*?)</h5>', re.S)


def _parse_dt(raw: str) -> datetime | None:
    try:
        return datetime.strptime(bn_digits(raw).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=DHAKA)
    except ValueError:
        return None


def parse_archive_fragment(fragment: str, *, source: str) -> list[FeedItem]:
    """One archive fragment to FeedItems, in listed order.

    An item with no parseable date keeps `published_at=None` rather than
    being dropped, matching every other family here: an undated article
    is filtered by `is_recent`'s three-valued check downstream, not
    silently discarded at parse time.
    """
    starts = [m.start() for m in _CARD_RE.finditer(fragment)]
    if not starts:
        return []
    bounds = list(zip(starts, starts[1:] + [len(fragment)]))

    items: list[FeedItem] = []
    seen: set[str] = set()
    for start, end in bounds:
        block = fragment[start:end]
        anchor = _ANCHOR_RE.search(block)
        if not anchor:
            continue
        url = anchor.group(1)
        if not url.startswith("http"):
            url = SITE_URL + url
        if url in seen:
            continue
        seen.add(url)
        date_match = _DATE_RE.search(block)
        title_match = _TITLE_RE.search(block)
        items.append(
            FeedItem(
                url=url,
                title=clean_title(title_match.group(1)) if title_match else "",
                source_key=source,
                published_at=_parse_dt(date_match.group(1)) if date_match else None,
                source_id=anchor.group(2),
            )
        )
    return items


@register
class AmarBartaSource(NewsSource):
    key = "amar_barta"
    display_name = "Amarbarta"

    def __init__(self) -> None:
        self.session = make_session(pool_size=6)

    def _walk_archive(self) -> list[FeedItem]:
        collected: list[FeedItem] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            resp = get_or_raise(
                self.session,
                SITE_URL + ARCHIVE_PATH,
                method="POST",
                data={"newsCount": str(page * 12)},
                source=self.key,
            )
            batch = parse_archive_fragment(resp.text, source=self.key)
            if not batch:
                break
            fresh = [i for i in batch if i.url not in seen]
            for i in fresh:
                seen.add(i.url)
            collected.extend(fresh)
            # Every item on this page is confidently old, so every later
            # page is older still. Stop rather than walking the archive.
            if batch and all(is_recent(i.published_at) is False for i in batch):
                break
        return [i for i in collected if is_recent(i.published_at) is not False]

    def iter_feed_items(self) -> list[FeedItem]:
        try:
            items = self._walk_archive()
        except SourceFetchError as exc:
            logger.warning(
                "source_fallback_triggered",
                extra={
                    "source": self.key,
                    "primary_method": "archive",
                    "fallback_method": "gnews",
                    "primary_error": str(exc),
                },
            )
            return fetch_gnews(self.session, FALLBACK_GNEWS_DOMAIN, source=self.key)
        if items:
            return items
        logger.warning(
            "source_fallback_triggered",
            extra={"source": self.key, "primary_method": "archive", "fallback_method": "gnews"},
        )
        return fetch_gnews(self.session, FALLBACK_GNEWS_DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

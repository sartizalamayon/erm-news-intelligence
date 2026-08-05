"""Source-plugin contract (docs/speed-techniques.md §4 point 1).

    "Same two required methods, `NewsSource`-style ABC. One file per
    source (sources/<site>.py), each declaring exactly one authoritative
    iter_feed_items() (sitemap/RSS/API — whichever is empirically best
    for that site) and one fetch_article(). A shared registry.py is the
    only wiring point."

This file defines that contract and its plain-data value objects only.
Owner: P1-FETCH. Do NOT add per-source logic, HTTP calls, retry/backoff,
or the shared `_get()` / `make_session()` helper here — those belong
alongside the first real source file P1-FETCH adds. A source's fallback
discovery method (if any) is an implementation detail *inside*
iter_feed_items(), triggered only on an empty result
(speed-techniques.md §4 point 4) — it is not a second ABC method.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class FeedItem:
    """One entry discovered by iter_feed_items(), before the full article
    body is fetched. A starting field set — P1-FETCH may extend it, but
    keep it a plain dataclass; ARCH-DB owns the persisted row shape.
    """

    url: str
    title: str
    source_key: str
    published_at: datetime | None = None
    # Native per-source id, for the (source_key, source_id) unique
    # constraint that makes dedup an index lookup rather than a table
    # scan (speed-techniques.md §4 point 5).
    source_id: str | None = None


@dataclass
class FetchedArticle:
    """Result of fetch_article(): a feed item plus its resolved body."""

    item: FeedItem
    body_text: str
    language: str | None = None


class NewsSource(ABC):
    """Every per-source module implements this exactly once.

    `key` must be a short, stable, unique identifier (e.g.
    "prothom_alo") — it is the source_key half of the dedup unique
    constraint and the registry's lookup key.
    """

    key: str

    @abstractmethod
    def iter_feed_items(self) -> list[FeedItem]:
        """Discover today's candidate articles via this source's one
        authoritative method. Must raise app.errors.SourceFetchError (not
        a bare exception) on failure so the batch runner can isolate it
        without a broad `except Exception`.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        """Fetch and parse one article's full body. Must raise
        app.errors.SourceFetchError on failure.
        """
        raise NotImplementedError

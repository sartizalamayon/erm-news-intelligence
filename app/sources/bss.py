"""Bangladesh Sangbad Sangstha (BSS) — the national news agency.

Google News is the PRIMARY method here, not a fallback, and that is a
deliberate choice made against the alternatives rather than a shortcut.
Checked live 2026-08-17:

    /sitemap-news.xml   soft 404 (HTML catch-all, HTTP 200) — the URL
                        this source was originally configured with, which
                        is why it has never returned anything
    /rss.xml, /feed     same soft 404
    /sitemap.xml        REAL, 1,045 URLs, but carries NO <lastmod> and
                        the article URLs (/section/414068/Slug) hold no
                        date either

    An article page carries no JSON-LD `datePublished`, no
    `article:published_time`, and no <time datetime=...>. There is no
    machine-readable publication date anywhere on this site.

Without a date, `is_recent` cannot tell today's national wire copy from
a story two years old, and this pipeline files articles by PUBLICATION
date. Dating 1,045 sitemap URLs would mean fetching and scraping every
article page on every run, for a date that is not reliably in the HTML.

Google News returns 200 items for this domain, every one dated. That is
a smaller slice than the full sitemap, but it is the slice we can date,
and a dated subset is worth more here than an undated superset.
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

GNEWS_DOMAIN = "bssnews.net"


@register
class BssSource(NewsSource):
    key = "bss"
    display_name = "BSS"

    def __init__(self) -> None:
        self.session = make_session(pool_size=6)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_gnews(self.session, GNEWS_DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

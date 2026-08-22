"""Kalbela — Bengali general daily. #4 by article volume.

Was fetched via `app.sources._curl_sitemap` (a TLS-mismatch workaround,
not a bot-detection bypass). As of 2026-08, Cloudflare serves a full JS
challenge to every request against the native sitemap regardless of
client/headers — a JS challenge cannot be solved by `curl`/`requests`,
only by executing JavaScript, which this codebase has no dependency for
(no `playwright`/`selenium`/`cloudscraper`). Switched to Google News
search instead, the same proven workaround already used for 9 other
sources whose own site is unfetchable — see `app.sources._gnews`. Real
tradeoff: gnews returns fewer, more recent items than a full native
sitemap, so this source's volume drops meaningfully from what it was
(it was a top-4 source by volume on the native sitemap).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

DOMAIN = "kalbela.com"


@register
class KalbelaSource(NewsSource):
    key = "kalbela"
    display_name = "Kalbela"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_gnews(self.session, DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)

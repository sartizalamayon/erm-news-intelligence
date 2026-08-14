"""WordPress REST API walker — Nagorik (#15 by volume) and, outside the
ported set, several smaller WP-powered outlets.

Ported from `collector_logic.iter_wp_rest` (:848-901): paginates
`/wp-json/wp/v2/posts?orderby=date&order=desc&per_page=100` up to 20
pages, stops once a page has nothing inside the rolling window. A `400`
response means "past the last page" (WP's normal end-of-pages signal,
not a failure).

`_embed=1` was carried over from legacy and has been REMOVED. It asks
WordPress to inline every post's author, featured media and taxonomy
terms — work this module never reads (nothing below touches
`_embedded`; only `title`, `link` and `date_gmt` are used). Measured
live against tob.news, 100 posts, one page:

    with    _embed=1   HTTP 200 in 40.29s
    without _embed     HTTP 200 in  5.04s

An 8x slowdown for data that is parsed and thrown away, and the direct
cause of "Times of Bangladesh — wp_rest unreachable" in the run panel:
40s exceeded the 20s timeout this function used to pass, so a healthy
source was reported as unreachable on every single run.
"""

from __future__ import annotations

import requests

from app.errors import SourceFetchError
from app.sources._util import clean_title, is_recent, parse_dt
from app.sources.base import FeedItem
from app.sources.http import get

MAX_PAGES = 20
PER_PAGE = 100


def fetch_wp_rest(session: requests.Session, site_url: str, *, source: str) -> list[FeedItem]:
    api_base = f"{site_url.rstrip('/')}/wp-json/wp/v2/posts"
    items: list[FeedItem] = []

    for page in range(1, MAX_PAGES + 1):
        params = {"orderby": "date", "order": "desc", "per_page": PER_PAGE, "page": page}
        response = get(
            session,
            api_base,
            source=source,
            params=params,
            headers={"Accept": "application/json"},
            # Falls through to http.DEFAULT_TIMEOUT's split
            # connect/read budget rather than a flat 20s: a 100-post
            # page is a genuinely large response (~2 MB) and deserves
            # read headroom, while a dead host still fails on connect
            # in 5s.
        )
        if response is None:
            if page == 1:
                raise SourceFetchError(f"wp_rest unreachable: {api_base}", source=source)
            break
        if response.status_code == 400:
            break  # past the last page — normal end
        if response.status_code != 200:
            if page == 1:
                raise SourceFetchError(f"wp_rest returned HTTP {response.status_code}: {api_base}", source=source)
            break
        try:
            posts = response.json()
        except ValueError:
            break
        if not isinstance(posts, list) or not posts:
            break

        page_had_recent = False
        for post in posts:
            title_obj = post.get("title", {})
            raw_title = title_obj.get("rendered", "") if isinstance(title_obj, dict) else str(title_obj)
            title = clean_title(raw_title)
            link = post.get("link", "")
            if not title or not link:
                continue
            dt = parse_dt(post.get("date_gmt")) if post.get("date_gmt") else None
            if is_recent(dt) is not False:
                page_had_recent = True
            items.append(FeedItem(url=link, title=title, source_key=source, published_at=dt))

        if not page_had_recent:
            break

    return items

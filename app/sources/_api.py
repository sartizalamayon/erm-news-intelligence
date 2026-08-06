"""Generic JSON news-API walker — Prothom Alo, Prothom Alo English (both
Quintype `advanced-search`, offset pagination) and The Financial Express
(cursor pagination), the three "api"-method sources.

Ported from `collector_logic.iter_api` (:638-736). Field-mapped via
config so the same walker serves multiple APIs without per-source code:
`items_path` (dotted key to the list), `title_field`, `url_field`,
`date_field`, `base_url` (prepended to relative links), `next_field`
(cursor pagination), `paginate` ("cursor" or "offset"), `offset_step`,
`max_pages`. Stops paginating once a whole page has nothing inside the
rolling window — newest-first APIs put older items on later pages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

from app.errors import SourceFetchError
from app.sources._util import clean_title, is_recent, parse_dt
from app.sources.base import FeedItem
from app.sources.http import get


@dataclass
class ApiConfig:
    url: str
    items_path: str = ""
    title_field: str = "title"
    url_field: str = "url"
    date_field: str = "publishedAt"
    base_url: str = ""
    next_field: str = "nextPageUrl"
    max_pages: int = 10
    paginate: str = "cursor"  # "cursor" | "offset"
    offset_step: int = 100
    headers: dict[str, str] = field(default_factory=dict)


def _with_offset(base: str, offset: int) -> str:
    parsed = urlparse(base)
    q = parse_qs(parsed.query)
    q["offset"] = [str(offset)]
    new_query = urlencode(q, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))


def _navigate(payload: object, items_path: str) -> list:
    data: object = payload
    if items_path:
        for key in items_path.split("."):
            if isinstance(data, dict):
                data = data.get(key, [])
            else:
                return []
    return data if isinstance(data, list) else []


def fetch_json_api(session: requests.Session, cfg: ApiConfig, *, source: str) -> list[FeedItem]:
    url: str | None = cfg.url
    offset = 0
    items: list[FeedItem] = []
    page = 0
    first_page_ok = False

    while url and page < cfg.max_pages:
        page += 1
        fetch_url = _with_offset(url, offset) if cfg.paginate == "offset" else url
        response = get(session, fetch_url, source=source, headers=cfg.headers or None, timeout=20)
        if response is None:
            if page == 1:
                raise SourceFetchError(f"api unreachable: {fetch_url}", source=source)
            break
        if response.status_code != 200:
            if page == 1:
                raise SourceFetchError(f"api returned HTTP {response.status_code}: {fetch_url}", source=source)
            break
        try:
            payload = response.json()
        except ValueError:
            if page == 1:
                raise SourceFetchError(f"api returned non-JSON: {fetch_url}", source=source) from None
            break
        first_page_ok = True

        rows = _navigate(payload, cfg.items_path)
        if not rows:
            break

        page_had_recent = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            title = clean_title(str(row.get(cfg.title_field, "")))
            link = str(row.get(cfg.url_field, "")).strip()
            if not title or not link:
                continue
            if cfg.base_url and link.startswith("/"):
                link = cfg.base_url.rstrip("/") + link
            raw_date = row.get(cfg.date_field)
            dt = parse_dt(raw_date) if raw_date else None
            if is_recent(dt) is not False:
                page_had_recent = True
            items.append(FeedItem(url=link, title=title, source_key=source, published_at=dt))

        if not page_had_recent:
            break
        if cfg.paginate == "offset":
            offset += cfg.offset_step
        else:
            nxt = payload.get(cfg.next_field) if isinstance(payload, dict) else None
            url = str(nxt) if nxt else None

    if not first_page_ok and page == 0:
        raise SourceFetchError(f"api never returned a page: {cfg.url}", source=source)
    return items

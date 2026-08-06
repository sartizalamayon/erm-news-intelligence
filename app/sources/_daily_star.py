"""Daily Star's Drupal node-ID descending walk (#9 by volume). Every
node is at `/node/{id}`, which 302s to the canonical article URL when
that ID is a published article. IDs are dense but not perfectly
time-ordered at day boundaries, so results are buffered and sorted.

Ported from `collector_logic.iter_daily_star_nodes` (:1414-1539). Date
parsing reads only the PUBLISH portion of the article-meta block (text
before "UPDATED") so an edited article doesn't get re-dated to its edit
time — legacy's own documented reasoning, kept verbatim.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import requests

from app.errors import SourceFetchError
from app.sources._util import DHAKA, clean_title, is_recent
from app.sources.base import FeedItem
from app.sources.http import get, get_or_raise

WORKERS = 12
BATCH_SIZE = 96
SCAN_DAYS_BACK = 3
TIME_BUDGET_SECONDS = 120.0  # legacy had no cap here beyond the "5 old items" heuristic;
# a wall-clock ceiling is added so one misbehaving day can't hang a whole batch run.

_MONTH_NAMES = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
_MONTHS = {m: i for i, m in enumerate(_MONTH_NAMES, 1)}
_NODE_ID_RE = re.compile(r'href="/[^"]*?-(\d{6,})"')
_META_BLOCK_RE = re.compile(r"block-article-meta-block(.*?)SHARE", re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ABS_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*([AP]M)", re.I)
_RELATIVE_RE = re.compile(r"(hour|minute|second|just now|ago)", re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)
_OG_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]+)"')


def _resolve_one(
    session: requests.Session, nid: int, site_url: str, *, source: str
) -> tuple[int, str, str, datetime] | None:
    url = f"{site_url}/node/{nid}"
    response = get(session, url, source=source, timeout=20, max_retries=0, allow_redirects=True)
    if response is None or response.status_code != 200:
        return None
    final = response.url
    tail = final.split("thedailystar.net", 1)[-1][:7] if "thedailystar.net" in final else ""
    if "/node/" in tail:
        return None
    html = response.text

    m = _META_BLOCK_RE.search(html)
    if not m:
        return None
    text = _WS_RE.sub(" ", _TAG_RE.sub(" ", m.group(1))).strip()
    pub = re.split(r"\bUPDATED\b", text, maxsplit=1, flags=re.I)[0]

    dt: datetime | None = None
    md = _ABS_DATE_RE.search(pub)
    if md:
        month = _MONTHS.get(md.group(2).lower())
        if month:
            try:
                d = date(int(md.group(3)), month, int(md.group(1)))
                hour, minute = 12, 0
                tm = _TIME_RE.search(pub)
                if tm:
                    hour = int(tm.group(1)) % 12
                    if tm.group(3).upper() == "PM":
                        hour += 12
                    minute = int(tm.group(2))
                dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=DHAKA)
            except ValueError:
                dt = None
    if dt is None and _RELATIVE_RE.search(pub):
        today = datetime.now(DHAKA).date()
        dt = datetime(today.year, today.month, today.day, 12, 0, tzinfo=DHAKA)
    if dt is None:
        return None

    title = ""
    hm = _H1_RE.search(html)
    if hm:
        title = clean_title(hm.group(1))
    if not title:
        om = _OG_TITLE_RE.search(html)
        if om:
            title = clean_title(om.group(1))

    return (nid, title, final, dt)


def fetch_daily_star_nodes(session: requests.Session, site_url: str, *, source: str) -> list[FeedItem]:
    homepage = get_or_raise(session, site_url, source=source, timeout=15)
    ids = [int(x) for x in _NODE_ID_RE.findall(homepage.text)]
    if not ids:
        raise SourceFetchError(f"daily_star_nodes: no node IDs found on homepage of {site_url}", source=source)
    max_id = max(ids)

    resolved: list[tuple[int, str, str, datetime]] = []
    newest_day: date | None = None
    nid = max_id
    start = time.monotonic()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        while nid > 0:
            if time.monotonic() - start > TIME_BUDGET_SECONDS:
                break
            batch = list(range(nid, max(0, nid - BATCH_SIZE), -1))
            nid -= BATCH_SIZE
            for result in pool.map(lambda i: _resolve_one(session, i, site_url, source=source), batch):
                if result is None:
                    continue
                resolved.append(result)
                d = result[3].astimezone(DHAKA).date()
                if newest_day is None or d > newest_day:
                    newest_day = d
            if newest_day is not None and resolved:
                old_count = sum(
                    1 for r in resolved if (newest_day - r[3].astimezone(DHAKA).date()).days >= SCAN_DAYS_BACK
                )
                if old_count >= 5:
                    break

    resolved.sort(key=lambda r: r[3], reverse=True)
    return [
        FeedItem(url=url, title=title, source_key=source, published_at=dt, source_id=str(nid))
        for (nid, title, url, dt) in resolved
        if is_recent(dt) is not False
    ]

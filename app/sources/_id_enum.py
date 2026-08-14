"""Generic numeric-ID enumeration — Daily Adin and Sangbad (#17, #18 by
volume): articles live at a sequential-ID URL (`/news/{id}`), walked
descending from the current max, resolved concurrently, filtered to the
rolling window.

Ported from `collector_logic.iter_id_enum` (:961-1226). Two
`date_source` variants are implemented: `"jsonld"` (Daily Adin, Sangbad,
Arthosuchak — with `date_tz_override` and `canonical_hop`/
`not_article_check` options) and `"bengali_date"` (Somoy Journal, Daily
ICT News — Bengali-digit `DD Month YYYY` text, no JSON-LD available on
those sites). Legacy also supports `jsonld_timefirst`, `calendar_dmy`,
`nextdata`, `time_element` for InfoTech/Deshpatrika — not ported (see
the task report's coverage list; combined volume under 0.05% of the
corpus, not worth a third/fourth/fifth date-parser variant this late).
Hard caps (`MAX_PROBE=500`, `MAX_CONSEC_FAIL=60`, `TIME_BUDGET=90s`)
carried forward verbatim: this is legacy's own "single most fragile
strategy family" note, and those caps are what keep one misconfigured
source from hanging the whole run.

This is the "articles within a source in parallel" half of the two-tier
fan-out (speed-techniques.md §4 point 3) for sources built this way —
the concurrency lives HERE, inside `iter_feed_items()`, bounded by both
this module's own `workers` and the process-wide semaphore every
`http.get()` call already respects.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from app.errors import SourceFetchError
from app.sources._util import DHAKA, clean_title, is_recent
from app.sources.base import FeedItem
from app.sources.http import get, get_or_raise

MAX_PROBE = 500
MAX_CONSEC_FAIL = 60
TIME_BUDGET_SECONDS = 90.0
SCAN_DAYS_BACK = 3
SAFETY_CAP = 600

_JSONLD_DATE_RE = re.compile(
    r'"datePublished"\s*:\s*"(\d{4})-(\d{2})-(\d{2})'
    r"(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?"
    r'(Z|[+-]\d{2}:?\d{2})?'
)
_JSONLD_HEADLINE_RE = re.compile(r'"headline"\s*:\s*"([^"]{5,})"')
_OG_TITLE_RE = re.compile(r'property="og:title"\s+content="([^"]+)"')
_CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]*)"')
_CANONICAL_RE_SIMPLE = re.compile(r'<link rel="canonical" href="(https?://[^"]+)"')

# Bengali-digit date, e.g. "১৮ জুন ২০২৬" or "১৮ জুন ২০২৬, ২৩:১৬" — no time
# element on these sites, so a bare date always resolves to noon Dhaka
# (matches legacy's own documented behavior, collector_logic.py:1124-1139).
_BN_DIGIT_TABLE = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
_BN_MONTHS = {
    "জানুয়ারি": 1,
    "ফেব্রুয়ারি": 2,
    "মার্চ": 3,
    "এপ্রিল": 4,
    "মে": 5,
    "জুন": 6,
    "জুলাই": 7,
    "আগস্ট": 8,
    "সেপ্টেম্বর": 9,
    "অক্টোবর": 10,
    "নভেম্বর": 11,
    "ডিসেম্বর": 12,
}
_BN_MONTH_ALT = "|".join(_BN_MONTHS)

# TWO orderings, because the sites using this parser do not agree.
#
#   DAY-FIRST    "১৪ আগস্ট ২০২৬"          (the only form legacy handled)
#   MONTH-FIRST  "শুক্রবার, আগস্ট ১৪, ২০২৬"  (what somoyjournal.com serves)
#
# Only the day-first form existed here, so Somoy Journal's date never
# matched, `_parse_bengali_date` returned None, and EVERY article it
# discovered was dropped as undated — a permanent silent zero, reported
# in the run panel as an unremarkable "0 fetched". Discovery was working
# the whole time: the homepage yields 45 valid ids, and article pages
# return HTTP 200 in ~1.1s.
#
# Separators are `[\s,]+` rather than `\s+` so the comma in the
# month-first form does not have to be special-cased, and the optional
# leading weekday ("শুক্রবার,") is simply not matched — the regex starts
# at the month.
_BENGALI_DATE_RE_DAY_FIRST = re.compile(r"([০-৯]{1,2})[\s,]+(" + _BN_MONTH_ALT + r")[\s,]+([০-৯]{4})")
_BENGALI_DATE_RE_MONTH_FIRST = re.compile(r"(" + _BN_MONTH_ALT + r")[\s,]+([০-৯]{1,2})[\s,]+([০-৯]{4})")


@dataclass
class IdEnumConfig:
    site_url: str
    article_pattern: str
    id_regex: str
    date_source: str = "jsonld"  # | "bengali_date"
    date_tz_override: str | None = None
    not_article_check: str = "canonical_is_homepage"  # | "no_time_element" | "no_jsonld" | "none"
    canonical_hop: bool = False
    no_follow_redirects: bool = False
    workers: int = 8
    batch_size: int = 40


def _parse_jsonld_date(html: str, tz_override: str | None) -> datetime | None:
    m = _JSONLD_DATE_RE.search(html)
    if not m:
        return None
    year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hour = int(m.group(4)) if m.group(4) else 12
    minute = int(m.group(5)) if m.group(5) else 0
    second = int(m.group(6)) if m.group(6) else 0
    offset = m.group(7)
    try:
        if offset in ("Z", "+00:00", "+0000"):
            return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        if tz_override == "dhaka" or offset is None:
            return datetime(year, month, day, hour, minute, second, tzinfo=DHAKA)
        return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None


def _parse_bengali_date(html: str) -> datetime | None:
    """Bengali-script date out of raw HTML, in either ordering.

    Day-first is tried first because it is the historical form and is
    unambiguous; month-first is the fallback. See the two regexes above
    for why both are needed.
    """
    for pattern, day_first in ((_BENGALI_DATE_RE_DAY_FIRST, True), (_BENGALI_DATE_RE_MONTH_FIRST, False)):
        m = pattern.search(html)
        if not m:
            continue
        try:
            if day_first:
                day, month_name, year_s = m.group(1), m.group(2), m.group(3)
            else:
                month_name, day, year_s = m.group(1), m.group(2), m.group(3)
            return datetime(
                int(year_s.translate(_BN_DIGIT_TABLE)),
                _BN_MONTHS[month_name],
                int(day.translate(_BN_DIGIT_TABLE)),
                12,
                0,
                tzinfo=DHAKA,
            )
        except (ValueError, KeyError):
            continue
    return None


def _is_article(html: str, not_article_check: str, site_url: str) -> bool:
    if not_article_check == "canonical_is_homepage":
        m = _CANONICAL_RE.search(html)
        if m:
            canon = m.group(1).rstrip("/")
            if canon == site_url.rstrip("/") or "/news/" not in canon:
                return False
        return True
    if not_article_check == "no_time_element":
        return "<time" in html
    if not_article_check == "no_jsonld":
        return '"datePublished"' in html
    return True  # "none" — rely on date-parse failure to reject non-articles


def _resolve_one(
    session: requests.Session, nid: int, cfg: IdEnumConfig, *, source: str
) -> tuple[int, str, str, datetime] | None:
    url = cfg.site_url + cfg.article_pattern.replace("{id}", str(nid))
    response = get(
        session, url, source=source, timeout=8, max_retries=0, allow_redirects=not cfg.no_follow_redirects
    )
    if response is None or response.status_code != 200:
        return None
    html = response.text
    final_url = response.url

    if cfg.canonical_hop:
        m = _CANONICAL_RE_SIMPLE.search(html)
        if not m:
            return None
        canon = m.group(1)
        if canon.rstrip("/") == cfg.site_url.rstrip("/"):
            return None
        canon_response = get(session, canon, source=source, timeout=8, max_retries=0)
        if canon_response is None or canon_response.status_code != 200:
            return None
        html = canon_response.text
        final_url = canon_response.url

    if not _is_article(html, cfg.not_article_check, cfg.site_url):
        return None

    if cfg.date_source == "bengali_date":
        dt = _parse_bengali_date(html)
    else:
        dt = _parse_jsonld_date(html, cfg.date_tz_override)
    if dt is None:
        return None

    title = ""
    hm = _JSONLD_HEADLINE_RE.search(html)
    if hm:
        title = clean_title(hm.group(1))
    if not title:
        om = _OG_TITLE_RE.search(html)
        if om:
            title = clean_title(om.group(1))

    return (nid, title, final_url, dt)


def fetch_id_enum(session: requests.Session, cfg: IdEnumConfig, *, source: str) -> list[FeedItem]:
    homepage = get_or_raise(session, cfg.site_url, source=source, timeout=15)
    all_ids = [int(m) for m in re.findall(cfg.id_regex, homepage.text)]
    if not all_ids:
        raise SourceFetchError(f"id_enum: no IDs found on homepage of {cfg.site_url}", source=source)
    max_id = max(all_ids)

    resolved: list[tuple[int, str, str, datetime]] = []
    newest_dt: datetime | None = None
    nid = max_id
    probed = 0
    consec_fail = 0
    start = time.monotonic()

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        while nid > 0 and probed < MAX_PROBE:
            if time.monotonic() - start > TIME_BUDGET_SECONDS:
                break
            batch = list(range(nid, max(0, nid - cfg.batch_size), -1))
            nid -= cfg.batch_size
            probed += len(batch)
            batch_hit = False
            for result in pool.map(lambda i: _resolve_one(session, i, cfg, source=source), batch):
                if result is None:
                    continue
                batch_hit = True
                resolved.append(result)
                if newest_dt is None or result[3] > newest_dt:
                    newest_dt = result[3]
            if batch_hit:
                consec_fail = 0
            else:
                consec_fail += len(batch)
                if consec_fail >= MAX_CONSEC_FAIL:
                    break
            if newest_dt is not None and resolved:
                cutoff = newest_dt - timedelta(days=SCAN_DAYS_BACK)
                old_count = sum(1 for r in resolved if r[3] < cutoff)
                if old_count >= 5:
                    break
            if len(resolved) > SAFETY_CAP:
                break

    resolved.sort(key=lambda r: r[3], reverse=True)
    return [
        FeedItem(url=url, title=title, source_key=source, published_at=dt, source_id=str(nid))
        for (nid, title, url, dt) in resolved
        if is_recent(dt) is not False
    ]

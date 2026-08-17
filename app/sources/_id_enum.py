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
from app.sources._util import BN_DIGIT_TABLE, BN_MONTH_ALT, BN_MONTHS, DHAKA, clean_title, is_recent
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

_ENGLISH_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_EN_MONTH_ALT = "January|February|March|April|May|June|July|August|September|October|November|December|" + "|".join(
    m.capitalize() for m in _ENGLISH_MONTHS
)

# `"datePublished": "18:35, 15 August 2026"` — TIME first, then the
# date, in Dhaka wall clock with no offset. Served by the eMythMakers
# CMS (InfoTech Insight, Share Biz, Share Business 24). `_JSONLD_DATE_RE`
# above only understands the ISO ordering and silently finds nothing
# here, which is why these sites need their own parser rather than a
# looser version of that one.
_JSONLD_TIMEFIRST_RE = re.compile(
    r'"datePublished"\s*:\s*"(\d{1,2}):(\d{2}),\s*(\d{1,2})\s+(' + _EN_MONTH_ALT + r")\s+(\d{4})\""
)

# `<i class="fa fa-calendar"></i>&nbsp;&nbsp;11-08-2026` — Desh Patrika
# prints a bare DD-MM-YYYY next to a calendar glyph and no clock time at
# all. Anchored on the glyph because a naked DD-MM-YYYY regex also
# matches the "related news" dates further down the same page (the live
# page carries five of them), which would date the article to whichever
# sidebar story happened to come first.
_CALENDAR_DMY_RE = re.compile(r"fa-calendar[^>]*>\s*</i>(?:\s|&nbsp;|&#160;)*(\d{1,2})-(\d{1,2})-(\d{4})")

# `<div class="entry_update ..."><i class="fa fa-clock-o"></i>&nbsp;&nbsp;17
# Aug 2026, 00:16<br></div>` — BSS's only machine-readable timestamp (no
# JSON-LD anywhere on the page). Scoped to the `entry_update` container
# for the same reason as above: the page's "latest news" rail repeats
# the same date format for other stories.
_ENTRY_UPDATE_BLOCK_RE = re.compile(r'class="[^"]*entry_update[^"]*"[^>]*>(.{0,400}?)</div>', re.S)
_ENGLISH_DMY_TIME_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4}),\s*(\d{1,2}):(\d{2})")

# An id far above every other id on the page is not an id.
#
# Daily ICT News' homepage embeds image filenames built from a UUID:
#
#     /content/archives/2026/08/9007442b-f8d6-4749-b568-a19c11a77876-bigthumb.jpg
#
# `(?:dailyictnews\.com)?/(\d{4,})` matched the leading `9007442` of that
# UUID, so `max(all_ids)` returned 9,007,442 against a real newest
# article id of 17,353. The walk started 8.99 MILLION ids above the top
# of the site, every probe 404'd, and the source hit MAX_CONSEC_FAIL and
# reported "0 fetched" after 57 seconds — run 125's Daily ICT News
# entry. Tightening that one regex fixes that one site; this guard fixes
# the whole family, because any source whose page embeds a long digit
# run anywhere can lose its entire run the same silent way.
#
# The test is relative to the MEDIAN, which is unmoved by a handful of
# outliers, and the multiplier is deliberately loose: real article ids
# on a homepage span at most a few thousand around the median, so a
# 100x gap is never a legitimate id and always a false match.
OUTLIER_ID_FACTOR = 100
MIN_IDS_FOR_OUTLIER_CHECK = 5

# Bengali-digit date, e.g. "১৮ জুন ২০২৬" or "১৮ জুন ২০২৬, ২৩:১৬" — no time
# element on these sites, so a bare date always resolves to noon Dhaka
# (matches legacy's own documented behavior, collector_logic.py:1124-1139).
# The digit table and month names live in `_util` because the archive
# listing family reads the same script.
_BN_DIGIT_TABLE = BN_DIGIT_TABLE
_BN_MONTHS = BN_MONTHS
_BN_MONTH_ALT = BN_MONTH_ALT

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
    # "jsonld" | "bengali_date" | "jsonld_timefirst" | "calendar_dmy" | "entry_update"
    date_source: str = "jsonld"
    date_tz_override: str | None = None
    not_article_check: str = "canonical_is_homepage"  # | "no_time_element" | "no_jsonld" | "none"
    canonical_hop: bool = False
    no_follow_redirects: bool = False
    workers: int = 8
    batch_size: int = 40
    # Some CMSes append the section to og:title ("Headline | Section").
    # Set the separator to split it back off, so the stored headline is
    # the headline and the section is available to `skip_sections`.
    title_section_separator: str | None = None
    # Sections to drop entirely, lower-cased. Needed where a site
    # publishes the SAME story twice under two different ids — BSS mirrors
    # every story into a "Subscriber" wire feed, so collecting both would
    # double every article it produces. Requires `title_section_separator`.
    skip_sections: frozenset[str] = frozenset()


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


def _parse_jsonld_timefirst_date(html: str) -> datetime | None:
    """`"datePublished": "18:35, 15 August 2026"` — Dhaka wall clock,
    no offset carried in the string (see `_JSONLD_TIMEFIRST_RE`).
    """
    m = _JSONLD_TIMEFIRST_RE.search(html)
    if not m:
        return None
    hour, minute, day, month_name, year = m.groups()
    month = _ENGLISH_MONTHS.get(month_name[:3].lower())
    if month is None:
        return None
    try:
        return datetime(int(year), month, int(day), int(hour), int(minute), tzinfo=DHAKA)
    except ValueError:
        return None


def _parse_calendar_dmy_date(html: str) -> datetime | None:
    """`fa-calendar` glyph followed by `DD-MM-YYYY`. The page carries no
    clock time, so a bare date resolves to noon Dhaka — the same
    convention `_parse_bengali_date` already uses for dateless pages.
    """
    m = _CALENDAR_DMY_RE.search(html)
    if not m:
        return None
    day, month, year = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, 12, 0, tzinfo=DHAKA)
    except ValueError:
        return None


def _parse_entry_update_date(html: str) -> datetime | None:
    """First `DD Mon YYYY, HH:MM` inside the `entry_update` container,
    Dhaka wall clock. A page with no such container (BSS serves a
    fixed placeholder for every unused id) yields `None`, which is what
    rejects it as a non-article.
    """
    block = _ENTRY_UPDATE_BLOCK_RE.search(html)
    if not block:
        return None
    m = _ENGLISH_DMY_TIME_RE.search(block.group(1))
    if not m:
        return None
    day, month_name, year, hour, minute = m.groups()
    month = _ENGLISH_MONTHS.get(month_name[:3].lower())
    if month is None:
        return None
    try:
        return datetime(int(year), month, int(day), int(hour), int(minute), tzinfo=DHAKA)
    except ValueError:
        return None


def _parse_date(html: str, cfg: IdEnumConfig) -> datetime | None:
    if cfg.date_source == "bengali_date":
        return _parse_bengali_date(html)
    if cfg.date_source == "jsonld_timefirst":
        return _parse_jsonld_timefirst_date(html)
    if cfg.date_source == "calendar_dmy":
        return _parse_calendar_dmy_date(html)
    if cfg.date_source == "entry_update":
        return _parse_entry_update_date(html)
    return _parse_jsonld_date(html, cfg.date_tz_override)


def _split_section(title: str, separator: str | None) -> tuple[str, str]:
    """("Headline | Section") -> ("Headline", "section"). Returns the
    title unchanged and an empty section when there is no separator to
    split on.
    """
    if not separator or separator not in title:
        return title, ""
    head, _, section = title.rpartition(separator)
    return head.strip(), section.strip().lower()


def _plausible_max_id(all_ids: list[int]) -> int:
    """The highest id that is actually an article id — see
    `OUTLIER_ID_FACTOR` for the UUID-fragment false match this exists to
    reject. With too few ids to judge, the plain maximum is returned:
    guessing at a distribution from three samples would be worse than
    the behaviour this replaces.
    """
    if len(all_ids) < MIN_IDS_FOR_OUTLIER_CHECK:
        return max(all_ids)
    ordered = sorted(all_ids)
    median = ordered[len(ordered) // 2]
    ceiling = median * OUTLIER_ID_FACTOR
    plausible = [i for i in ordered if i <= ceiling]
    return max(plausible) if plausible else max(all_ids)


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

    dt = _parse_date(html, cfg)
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

    title, section = _split_section(title, cfg.title_section_separator)
    if section and section in cfg.skip_sections:
        return None

    return (nid, title, final_url, dt)


def fetch_id_enum(session: requests.Session, cfg: IdEnumConfig, *, source: str) -> list[FeedItem]:
    homepage = get_or_raise(session, cfg.site_url, source=source, timeout=15)
    all_ids = [int(m) for m in re.findall(cfg.id_regex, homepage.text)]
    if not all_ids:
        raise SourceFetchError(f"id_enum: no IDs found on homepage of {cfg.site_url}", source=source)
    max_id = _plausible_max_id(all_ids)

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

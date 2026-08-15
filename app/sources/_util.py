"""Small, dependency-free helpers shared by every method-family module
and per-source file: title cleanup, date parsing, the rolling "today"
window check, and URL normalization for dedup.

Ported from `backend/collector_logic.py` (READ-ONLY legacy reference) —
`clean_title` (:32-49), `parse_dt` (:546-568), `date_from_url` (:570-584),
`is_today` (:586-592), `normalize_url` (:2055-2080) — with one documented
fix: `NOW` is computed PER CALL here, not once at import time
(legacy-behavior.md §7 item 11: legacy's `NOW = datetime.now(DHAKA)` at
module import time silently compares every run against the timestamp the
*server process* started, not the current wall clock — a live bug this
rebuild must not reproduce).

No `bs4`/`lxml` — neither is an installed dependency of this project
(checked: not in `pyproject.toml`, not in `.venv`; flagged in the P1-FETCH
task report rather than added unilaterally). `clean_title`'s HTML-stripping
uses stdlib `html.parser` instead.
"""

from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DHAKA = timezone(timedelta(hours=6))
WINDOW = timedelta(hours=30)  # rolling "today" window, same as legacy

# How far into the future a "publication" date may be before we stop
# believing it.
#
# `is_recent` used to test only `(now - dt) <= WINDOW`. For a future
# date that difference is NEGATIVE, which is always <= WINDOW, so an
# article dated months ahead sailed through as "recent" — verified: a
# date 60 days in the future returned True. Two real rows reached the
# database this way, both Somoy Journal event pages where the parser
# picked up the date of the EVENT being announced rather than the date
# the story was published, landing them under 2026-09-15 and
# 2026-10-30 while actually collected on 2026-08-05.
#
# The tolerance is DAYS, not hours, because a newspaper legitimately
# publishes tomorrow's edition tonight. Verified in real data:
#
#   BD Pratidin    published 2026-08-01 06:00  fetched 2026-07-31 20:35
#   Daily Star     published 2026-08-01 12:00  fetched 2026-07-31 20:37
#   Daily Inqilab  published 2026-08-01 00:03  fetched 2026-07-31 20:36
#
# 1,222 rows follow that pattern, spread across many outlets and always
# exactly one day ahead. Those are EDITION dates, not broken parses, and
# the edition date is the date a reader means. Rejecting them would
# throw away a day of real news every night.
#
# What this is actually guarding against is the other 2 rows: Somoy
# Journal event-announcement pages where the parser picked up the date
# of the event being announced (41 and 86 days out) instead of the
# publication date.
FUTURE_TOLERANCE = timedelta(days=2)

# Entity decoding used to be this hand-written map of seven:
#
#   &amp; &lt; &gt; &#039; &apos; &quot; &nbsp;
#
# Everything else survived into the database as literal text, so 220
# stored headlines read `Israeli PM Netanyahu labels UK &#8216;Islamic
# Republic of Britain&#8217;` and `র&zwj;্যাগিং`. The frontend escapes
# titles (correctly, they are remote input), which means the entity is
# displayed rather than rendered, so the reader sees the raw code.
#
# The map also decoded in dict order, so `&amp;lt;` became `&lt;` on the
# first replacement and then `<` on the second. A double decode turns
# deliberately-escaped text back into markup, which is the wrong
# direction for anything downstream that trusts this output.
#
# `html.unescape` covers the full HTML5 named and numeric set and makes
# exactly ONE pass, so it fixes both. Kept as a named indirection
# because clean_title's contract is "no entities out", not "call this
# specific stdlib function".
_unescape_entities = html.unescape


class _TextStripper(HTMLParser):
    """Collects visible text, discarding all tags/attributes. Used only
    to turn a markup-wrapped title into plain text — NOT a general HTML
    body parser (see app/sources/_body.py for that).
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return " ".join(self._parts)


def clean_title(raw: str | None) -> str:
    """Strip HTML/CDATA wrapping, unescape entities, collapse whitespace.
    Mirrors `collector_logic.clean_title()` (legacy-behavior.md §2.6
    step 1) — some feeds wrap titles in markup (e.g. Daily Star), which
    would otherwise render as raw `<a>` links downstream.

    Titles that arrive as markup are decoded by the stripper below
    (HTMLParser resolves charrefs as it parses). Titles that arrive as
    PLAIN text with entities in them reach `_unescape_entities`, and
    that is the path the old seven-entity map got wrong — see its
    comment above.
    """
    if not raw:
        return ""
    text = str(raw)
    if "<" in text and ">" in text:
        try:
            stripper = _TextStripper()
            stripper.feed(text)
            text = stripper.text()
        except Exception:
            text = re.sub(r"<[^>]+>", " ", text)
    text = _unescape_entities(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_dt(value: str | int | float | None) -> datetime | None:
    """Parse a date/time value from many shapes a feed might use: epoch
    seconds/milliseconds (Quintype/Prothom Alo), ISO-8601 (with a
    trailing `Z`), or RFC-2822 (`email.utils`). Returns `None` on
    anything unparseable rather than raising — a source with one weird
    date format must not crash discovery for every other article.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            n = int(value)
            if n > 1_000_000_000_000:  # milliseconds
                n = n // 1000
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    for parser in (
        lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")),
        parsedate_to_datetime,
    ):
        try:
            dt = parser(text)
            if dt is not None:
                return dt
        except (ValueError, TypeError):
            continue
    return None


_URL_DATE_RE = re.compile(r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:[/-]|$)")


def date_from_url(url: str | None) -> datetime | None:
    """Extract a publish date from a URL path like `/2026/06/16/` when
    the document itself supplies no date. Returns `None` for URLs with
    no recognizable date (e.g. numeric-ID URLs).
    """
    if not url:
        return None
    m = _URL_DATE_RE.search(url)
    if not m:
        return None
    year, month, day = (int(g) for g in m.groups())
    try:
        return datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None


def is_recent(dt: datetime | None, *, now: datetime | None = None) -> bool | None:
    """Three-valued "is this inside the rolling WINDOW" check — `True`
    (inside), `False` (confidently old), `None` (undated, unknown).
    `now` defaults to a FRESH `datetime.now(DHAKA)` computed at call
    time, never a module-level constant (legacy-behavior.md §7 item 11's
    stale-NOW bug).
    """
    if dt is None:
        return None
    reference = now if now is not None else datetime.now(DHAKA)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age = reference - dt.astimezone(DHAKA)
    if age < -FUTURE_TOLERANCE:
        # Dated meaningfully in the FUTURE. Not "recent" — not real.
        # Returning False (confidently reject) rather than None
        # (undated, keep) is the point: `None` would keep the row and
        # file it under a future date, which is what let two
        # event-announcement pages land months ahead of today.
        return False
    return age <= WINDOW


# Fix for docs/red-team-findings.md RT-3: only "utm_" is a genuine
# PREFIX (utm_source, utm_campaign, ...) — every other entry is a
# specific, exact query-param NAME. The original implementation applied
# `.startswith()` uniformly to all of them, which silently stripped any
# param merely starting with one of those strings (`referral_code`,
# `refresh_token`, `gclidextra`, `fbclid2`, `spmid`, `cmpidx`, ...) —
# none of which are tracking params — collapsing distinct article URLs
# to the same `url_norm` and causing `ON CONFLICT DO NOTHING` to silently
# drop a genuinely different article as a false-positive duplicate.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_EXACT = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid", "spm", "cmpid"})


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith(_TRACKING_PARAM_PREFIXES) or lowered in _TRACKING_PARAM_EXACT


def normalize_url(url: str) -> str:
    """Clean a URL so the same article fetched via different methods (or
    re-fetched on a later day) dedupes to the same key. Ported from
    `collector_logic.normalize_url()`: unwraps Google News redirect
    links, forces https, drops `www.`, strips tracking query params,
    drops the trailing slash and any `#fragment`. Used ONLY for the
    `url_norm` dedup column — the original display URL is kept as-is.

    Tracking-param matching: only `utm_*` is a genuine prefix; every
    other name in `_TRACKING_PARAM_EXACT` must match exactly (RT-3 fix
    — see `_is_tracking_param` and the comment above
    `_TRACKING_PARAM_PREFIXES`).
    """
    if not url:
        return url
    try:
        if "news.google." in url:
            q = dict(parse_qsl(urlparse(url).query))
            if q.get("url"):
                url = q["url"]
        parsed = urlparse(url)
        netloc = parsed.netloc.replace("www.", "").lower()
        path = parsed.path.rstrip("/")
        kept = [(k, v) for k, v in parse_qsl(parsed.query) if not _is_tracking_param(k)]
        return urlunparse(("https", netloc, path, "", urlencode(kept), ""))
    except Exception:
        return url


def today_dhaka() -> date:
    """The COLLECTION day (legacy-behavior.md §6: `day` = `date.today()`
    at run start, Dhaka-local — not necessarily the article's own
    published day). Computed per-call, same stale-NOW fix as `is_recent`.
    """
    return datetime.now(DHAKA).date()

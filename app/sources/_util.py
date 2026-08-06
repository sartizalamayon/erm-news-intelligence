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

import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DHAKA = timezone(timedelta(hours=6))
WINDOW = timedelta(hours=30)  # rolling "today" window, same as legacy

_ENTITY_MAP = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&#039;": "'",
    "&apos;": "'",
    "&quot;": '"',
    "&nbsp;": " ",
}


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
    Mirrors `collector_logic.clean_title()` exactly (legacy-behavior.md
    §2.6 step 1) — some feeds wrap titles in markup (e.g. Daily Star),
    which would otherwise render as raw `<a>` links downstream.
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
    for entity, replacement in _ENTITY_MAP.items():
        text = text.replace(entity, replacement)
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
    return (reference - dt.astimezone(DHAKA)) <= WINDOW


_TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src", "igshid", "spm", "cmpid")


def normalize_url(url: str) -> str:
    """Clean a URL so the same article fetched via different methods (or
    re-fetched on a later day) dedupes to the same key. Ported verbatim
    from `collector_logic.normalize_url()`: unwraps Google News redirect
    links, forces https, drops `www.`, strips tracking query params,
    drops the trailing slash and any `#fragment`. Used ONLY for the
    `url_norm` dedup column — the original display URL is kept as-is.
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
        kept = [
            (k, v)
            for k, v in parse_qsl(parsed.query)
            if not any(k.lower().startswith(bad) or k.lower() == bad for bad in _TRACKING_PARAM_PREFIXES)
        ]
        return urlunparse(("https", netloc, path, "", urlencode(kept), ""))
    except Exception:
        return url


def today_dhaka() -> date:
    """The COLLECTION day (legacy-behavior.md §6: `day` = `date.today()`
    at run start, Dhaka-local — not necessarily the article's own
    published day). Computed per-call, same stale-NOW fix as `is_recent`.
    """
    return datetime.now(DHAKA).date()

"""News-sitemap XML — the highest-volume method family (6 of the 21
ported sources: Daily Inqilab, Dhaka Post, Desh Rupantor, The Business
Standard, Dhaka Mail, CNN — see docs/legacy-behavior.md §2.2).

Ported from `backend/collector_logic.py::_sitemap_from_url` (:1684-1741)
and `iter_sitemap`/`_sitemap_candidates` (:1751-1793). Follows one level
of sitemap-index automatically, reads
`<news:publication_date>` -> `<lastmod>` -> date-from-URL-path as a date
fallback chain, and drops confidently-old articles inline (same
three-valued `is_recent()` check as legacy's `is_today()`).

Deliberately DOES use `ElementTree` (a real, if minimal, DOM build) here
rather than speed-techniques.md §3#8's "skip DOM entirely, 4 precompiled
regexes" suggestion. That technique is explicitly the lowest-ranked
"Adopt" item (5-10% estimated, sitemap-heavy sources only) and assumes
`lxml`; without it, hand-rolled regex across the ~9 different sitemap
dialects encountered below (namespaced Google-News tags, bare `<url>`
sets, sitemap-index children) is a correctness risk this task's "rigid,
unbreakable" goal isn't worth trading for a single-digit-percent gain.
Flagged in the task report, not silently deviated from.
"""

from __future__ import annotations

from datetime import datetime

import requests

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._util import clean_title, date_from_url, is_recent, parse_dt
from app.sources._xml import first_child_text, local_tag, parse_xml
from app.sources.base import FeedItem
from app.sources.http import get

logger = get_logger(__name__)

# Common news-sitemap path patterns to try when the exact URL is unknown
# or has moved — legacy's NEWS_SITEMAP_PATHS (collector_logic.py:1745-1749).
NEWS_SITEMAP_PATHS = [
    "/sitemap-news.xml",
    "/news-sitemap.xml",
    "/sitemap_news.xml",
    "/news.xml",
    "/google-news-sitemap.xml",
    "/sitemaps/news-sitemap.xml",
    "/sitemap/sitemap-news.xml",
    "/sitemap/news.xml",
    "/rss/news-sitemap.xml",
]


def parse_urlset(content: bytes, *, cap: int) -> list[tuple[str, str, datetime | None]]:
    """Parse one `<urlset>` document into (title, url, dt) tuples,
    dropping anything confidently outside the rolling window.
    """
    root = parse_xml(content)
    if root is None:
        return []
    out: list[tuple[str, str, datetime | None]] = []
    for url_el in root:
        if local_tag(url_el.tag) != "url":
            continue
        if len(out) >= cap:
            break
        loc = first_child_text(url_el, "loc")
        if not loc:
            continue
        title = clean_title(first_child_text(url_el, "title") or "")
        date_text = first_child_text(url_el, "publication_date", "lastmod")
        dt = parse_dt(date_text) if date_text else None
        if dt is None:
            dt = date_from_url(loc)
        if is_recent(dt) is False:
            continue
        out.append((title, loc, dt))
    return out


def _resolve_index_child(content: bytes) -> str | None:
    """If `content` is a `<sitemapindex>`, return the first news-looking
    child sitemap URL (preferring one whose URL contains "news"); `None`
    if it isn't an index or has no children.
    """
    if b"<sitemapindex" not in content[:3000]:
        return None
    root = parse_xml(content)
    if root is None:
        return None
    candidates = []
    for sm in root:
        if local_tag(sm.tag) != "sitemap":
            continue
        loc = first_child_text(sm, "loc")
        if loc:
            candidates.append(loc)
    for c in candidates:
        if "news" in c.lower():
            return c
    return candidates[0] if candidates else None


def fetch_one_sitemap_url(
    session: requests.Session, url: str, *, source: str, cap: int = 600, timeout: float = 30.0
) -> list[FeedItem]:
    """Fetch ONE news-sitemap XML URL, following one level of
    sitemap-index automatically. Returns [] on any fetch/parse failure
    (a candidate that doesn't pan out, not a hard error) — the caller
    decides whether that means "try the next candidate" or "this source
    is empty today".
    """
    response = get(session, url, source=source, timeout=timeout)
    if response is None or response.status_code != 200:
        return []
    content = response.content
    child_url = _resolve_index_child(content)
    if child_url:
        child_response = get(session, child_url, source=source, timeout=timeout)
        if child_response is None or child_response.status_code != 200:
            return []
        content = child_response.content
    tuples = parse_urlset(content, cap=cap)
    return [FeedItem(url=u, title=t, source_key=source, published_at=dt) for (t, u, dt) in tuples]


def _sitemap_candidates(session: requests.Session, primary_url: str, *, source: str) -> list[str]:
    """Ordered candidate list: the configured URL first, then any
    robots.txt-declared sitemap (news ones prioritized), then the common
    path patterns — mirrors `_sitemap_candidates()`
    (collector_logic.py:1751-1778). This is resilience WITHIN the single
    "sitemap" method (finding the right URL for it), not a second
    discovery method — distinct from the always-on RSS/gnews union this
    rebuild deliberately does not port (speed-techniques.md §4 point 4).
    """
    from urllib.parse import urlparse

    candidates = [primary_url]
    parsed = urlparse(primary_url)
    root = f"{parsed.scheme}://{parsed.netloc}"

    robots = get(session, root + "/robots.txt", source=source, timeout=8, max_retries=0)
    if robots is not None and robots.status_code == 200:
        for line in robots.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                if "news" in sm_url.lower():
                    candidates.insert(1, sm_url)
                else:
                    candidates.append(sm_url)

    for path in NEWS_SITEMAP_PATHS:
        candidates.append(root + path)

    seen: set[str] = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def fetch_news_sitemap(
    session: requests.Session,
    url: str,
    *,
    source: str,
    cap: int = 600,
    max_candidates: int = 5,
) -> list[FeedItem]:
    """The authoritative "sitemap" method: try the configured URL first
    (full 30s budget, exactly ONE request on the happy path). Only if
    that comes back empty does this build and try a bounded list of
    fallback candidates (robots.txt-declared sitemap + common paths,
    each with a short 8s timeout so a genuinely dead source fails fast)
    — speed-techniques.md §4 point 4's "fallback fires only when primary
    returns zero items", applied to the candidate-discovery cost itself,
    not just the final method choice. Returns the first candidate that
    yields any items; [] if none do (the source is then reported empty —
    no further method is tried here).
    """
    primary_items = fetch_one_sitemap_url(session, url, source=source, cap=cap, timeout=30.0)
    if primary_items:
        return primary_items

    candidates = [c for c in _sitemap_candidates(session, url, source=source) if c != url][: max_candidates - 1]
    for candidate in candidates:
        items = fetch_one_sitemap_url(session, candidate, source=source, cap=cap, timeout=8.0)
        if items:
            logger.warning(
                "sitemap_fallback_candidate_used",
                extra={"source": source, "primary_url": url, "used_url": candidate},
            )
            return items
    return []


def require_news_sitemap(session: requests.Session, url: str, *, source: str, cap: int = 600) -> list[FeedItem]:
    """Like `fetch_news_sitemap`, but raises `SourceFetchError` if the
    primary URL is unreachable — for sources whose ABC method should
    treat total unreachability as a per-source failure rather than a
    silent empty result (docs/red-team-findings.md RT-2: a dead host
    reporting `status="empty"` is byte-for-byte indistinguishable from
    "genuinely no news today"). Candidate fallback still applies before
    giving up.

    "Unreachable" is deliberately broader than "no response at all"
    (a bare network failure): a definitive non-200 (404, 500, ...) on
    the reachability probe counts too. A host that answers every one of
    the primary URL, robots.txt, and every `NEWS_SITEMAP_PATHS`
    candidate with 404 has no working sitemap at any known location —
    that is a real failure worth a human's attention, not a quiet
    "empty" that looks identical to a slow news day.
    """
    items = fetch_news_sitemap(session, url, source=source, cap=cap)
    if not items:
        # Confirm the primary URL is actually reachable (and actually
        # answers 200) before treating this as "empty feed" rather than
        # "source is down".
        response = get(session, url, source=source, timeout=10, max_retries=1)
        if response is None or response.status_code != 200:
            raise SourceFetchError(f"sitemap unreachable: {url}", source=source)
    return items

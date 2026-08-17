"""News-sitemap fetched via the system `curl` binary — a pure transport
workaround for Kalbela and Daily Sun (#4 and #13 by volume), whose
Cloudflare edges negotiate TLS 1.3 in a way Python's bundled SSL can't
match (they answer `requests` with 403, but serve curl/real browsers
normally). Ported from `collector_logic._curl_get`/`iter_curl_sitemap`
(:1366-1411). No proxy, no challenge-solving — just a different HTTP
client for exactly these two known hosts.

`curl` is a system binary already present on the target platforms
(macOS/Linux dev + CI, Windows 10+ ships it), not a new Python
dependency — matching legacy's own justification verbatim.
"""

from __future__ import annotations

import subprocess
import time

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._sitemap import parse_urlset
from app.sources._util import clean_title
from app.sources.base import FeedItem
from app.sources.http import USER_AGENT

logger = get_logger(__name__)

# How many times to re-ask when the edge hands back an interstitial
# instead of the sitemap, and how long to wait between tries.
#
# Measured live 2026-08-17, eight consecutive requests per host:
#
#     kalbela.com/sitemap/news-sitemap.xml    5 x 200 (66 KB),  3 x 403
#     daily-sun.com/news_sitemap.xml          5 x 200 (44 KB),  3 x 403
#
# The challenge is issued PER REQUEST, not per client — the same curl
# invocation that just got a 403 succeeds moments later. One attempt
# therefore fails outright about a third of the time, which is what
# both of these sources had been doing. Four attempts put the odds of
# an all-challenge run at roughly 2%, and a genuinely dead host still
# costs only four fast failures.
MAX_ATTEMPTS = 4
RETRY_SLEEP_SECONDS = 1.5


def _curl_get(url: str, *, timeout: float = 30.0) -> str:
    """Returns the response body text, or "" on any failure (missing
    curl binary, timeout, non-decodable response) — matches legacy's
    "never blocks the caller" contract.
    """
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sL",
                "--compressed",
                "--max-time",
                str(int(timeout)),
                "-A",
                USER_AGENT,
                "-H",
                "Accept-Language: en-US,en;q=0.9,bn;q=0.8",
                url,
            ],
            capture_output=True,
            timeout=timeout + 10,
            check=False,
        )
        return proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        logger.warning("curl_get_failed", extra={"url": url, "error": type(exc).__name__})
        return ""


def _is_sitemap_document(text: str) -> bool:
    """Whether curl actually returned the sitemap we asked for.

    An empty body is the "host unreachable / curl missing" case this
    module always handled. The case it did NOT handle is an edge that
    answers with a full HTTP 200-or-403 HTML body — Cloudflare's "Just a
    moment..." interstitial — which is neither empty nor a sitemap.
    `parse_urlset` finds no `<url>` elements in it and returns [], so a
    BLOCKED fetch was reported as `status="empty"`: byte-for-byte the
    same as a quiet news day (docs/red-team-findings.md RT-2, the exact
    failure mode `require_news_sitemap` exists to prevent on the
    `requests` path).

    Checking for the root element rather than for challenge markers
    keeps this a positive test: any body that is not the document we
    asked for is a failure, whatever the edge decides to serve next
    year.
    """
    head = text[:4000].lower()
    return "<urlset" in head or "<sitemapindex" in head


def fetch_curl_sitemap(url: str, *, source: str, cap: int = 600, timeout: float = 30.0) -> list[FeedItem]:
    """Same `<news:publication_date>` parsing as `fetch_news_sitemap`,
    just fetched over curl instead of `requests`.

    Retries while the edge returns something that is not a sitemap (see
    `_is_sitemap_document` and `MAX_ATTEMPTS`), and raises
    `SourceFetchError` once the attempts are spent — a blocked source
    must surface as a failure, never as a successful empty fetch.
    Returns [] only when a REAL sitemap parsed and simply had nothing
    recent in it.
    """
    xml_text = ""
    for attempt in range(MAX_ATTEMPTS):
        xml_text = _curl_get(url, timeout=timeout)
        if _is_sitemap_document(xml_text):
            break
        logger.warning(
            "curl_sitemap_not_a_sitemap",
            extra={
                "source": source,
                "url": url,
                "attempt": attempt + 1,
                "bytes": len(xml_text),
                "diagnosis": _diagnose(xml_text),
            },
        )
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(RETRY_SLEEP_SECONDS)
    else:
        raise SourceFetchError(
            f"curl sitemap fetch failed after {MAX_ATTEMPTS} attempts: {url} — {_diagnose(xml_text)}",
            source=source,
        )

    tuples = parse_urlset(xml_text.encode("utf-8"), cap=cap)
    return [
        FeedItem(url=u, title=t or _title_from_slug(u), source_key=source, published_at=dt) for (t, u, dt) in tuples
    ]


def _diagnose(text: str) -> str:
    """One phrase an operator can act on, instead of "0 fetched"."""
    if not text.strip():
        return "no response body (host unreachable, or curl unavailable)"
    lowered = text[:4000].lower()
    if "just a moment" in lowered or "challenge-platform" in lowered or "cf-chl" in lowered:
        return "edge served a Cloudflare JS challenge instead of the sitemap"
    if "<html" in lowered:
        return "server returned an HTML page, not a sitemap"
    return "response was not a sitemap document"


def _title_from_slug(url: str) -> str:
    """When the sitemap omits `<news:title>`, derive a placeholder from
    the URL slug — matches legacy's fallback (collector_logic.py:1406-1409).
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return clean_title(slug.replace("-", " "))[:200]

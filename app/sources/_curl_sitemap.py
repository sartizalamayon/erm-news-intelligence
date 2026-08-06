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

from app.errors import SourceFetchError
from app.logging import get_logger
from app.sources._sitemap import parse_urlset
from app.sources._util import clean_title
from app.sources.base import FeedItem
from app.sources.http import USER_AGENT

logger = get_logger(__name__)


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


def fetch_curl_sitemap(url: str, *, source: str, cap: int = 600, timeout: float = 30.0) -> list[FeedItem]:
    """Same `<news:publication_date>` parsing as `fetch_news_sitemap`,
    just fetched over curl instead of `requests`. Raises
    `SourceFetchError` when curl produced no body at all (source down /
    curl missing); returns [] when curl succeeded but the sitemap simply
    has nothing (or nothing recent).
    """
    xml_text = _curl_get(url, timeout=timeout)
    if not xml_text.strip():
        raise SourceFetchError(f"curl sitemap fetch produced no content: {url}", source=source)
    tuples = parse_urlset(xml_text.encode("utf-8"), cap=cap)
    return [
        FeedItem(url=u, title=t or _title_from_slug(u), source_key=source, published_at=dt) for (t, u, dt) in tuples
    ]


def _title_from_slug(url: str) -> str:
    """When the sitemap omits `<news:title>`, derive a placeholder from
    the URL slug — matches legacy's fallback (collector_logic.py:1406-1409).
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return clean_title(slug.replace("-", " "))[:200]

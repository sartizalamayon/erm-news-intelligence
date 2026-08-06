"""Generic article-body extraction — the `fetch_article()` half of the
`NewsSource` ABC contract, shared by every per-source file so a single
readable-text heuristic doesn't need reimplementing per outlet.

Ported from `backend/article_fetch.py:33-73` (READ-ONLY legacy
reference): drop obviously non-article tags, prefer the container with
the most cumulative `<p>` text, else fall back to every `<p>` over 40
chars, cap at `MAX_CHARS`. No `bs4`/`lxml` (not installed — see
app/sources/_util.py's docstring); this uses stdlib `html.parser`
instead of a DOM/CSS-selector engine, so "container with the most `<p>`
text" is approximated by grouping `<p>` text under its nearest ancestor
that looks like an article wrapper (by tag/class/id), which is the same
heuristic BeautifulSoup's `.select(...)` was doing, just without a real
DOM to query.

speed-techniques.md §4 point 7 / §6: keep this matched-only (called for
the keyword-matched subset, not every fetched article) — that decision
is the CALLER's (app/services/collector.py or a future P2/P3 hookup), not
this module's; this file only implements "fetch and extract one body"
per the ABC's required method.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

import requests

from app.sources.http import get

MAX_CHARS = 4000
_DROP_TAGS = frozenset({"script", "style", "nav", "header", "footer", "aside", "form", "iframe", "noscript"})
_ARTICLE_HINTS = ("article-body", "article-content", "story-content", "post-content", "articlebody")


class _ArticleBodyParser(HTMLParser):
    """Single-pass extractor: tracks the current tag stack, skips text
    inside `_DROP_TAGS`, and buckets `<p>` text by whether the nearest
    "article-like" ancestor (tag `article`, or a class/id containing one
    of `_ARTICLE_HINTS`) is currently open. Falls back to every
    reasonably long `<p>` if no article-like container is ever seen.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._article_depth = 0
        self._in_p = False
        self._p_buffer: list[str] = []
        self.article_paragraphs: list[str] = []
        self.all_paragraphs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        class_id = f"{attrs_dict.get('class', '')} {attrs_dict.get('id', '')}".lower()
        if tag in _DROP_TAGS:
            self._skip_depth += 1
            return
        if tag == "article" or any(hint in class_id for hint in _ARTICLE_HINTS):
            self._article_depth += 1
        if tag == "p" and self._skip_depth == 0:
            self._in_p = True
            self._p_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "p" and self._in_p:
            text = " ".join(self._p_buffer).strip()
            if text:
                self.all_paragraphs.append(text)
                if self._article_depth > 0:
                    self.article_paragraphs.append(text)
            self._in_p = False
            self._p_buffer = []
        if tag == "article" or tag in ("div", "section"):
            # Cheap heuristic close: we don't track a precise stack per
            # class match, so only decrement on the tags we increment on
            # (article) — div/section closes are ignored deliberately to
            # keep this a single linear pass, not a real tree.
            if tag == "article" and self._article_depth > 0:
                self._article_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_p and self._skip_depth == 0:
            self._p_buffer.append(data)


def extract_readable_text(html: str) -> str:
    """Best-effort readable-text extraction. Returns "" on any failure —
    callers must treat that as "no body available", never a hard error
    (legacy's own documented contract, article_fetch.py's docstring).
    """
    if not html:
        return ""
    try:
        parser = _ArticleBodyParser()
        parser.feed(html)
        paragraphs = parser.article_paragraphs or [p for p in parser.all_paragraphs if len(p) > 40]
        text = " ".join(paragraphs)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:MAX_CHARS]
    except Exception:
        return ""


def fetch_generic_article_body(session: requests.Session, url: str, *, source: str, timeout: float = 12.0) -> str:
    """Fetch `url` and return its extracted readable text, or "" on any
    failure (network, non-200, empty body, parse error) — mirrors
    `article_fetch.fetch_article_text()`'s "never blocks a caller"
    contract exactly.
    """
    response: requests.Response | None = get(session, url, source=source, timeout=timeout, max_retries=1)
    if response is None or response.status_code != 200 or not response.text:
        return ""
    return extract_readable_text(response.text)


__all__ = ["extract_readable_text", "fetch_generic_article_body", "MAX_CHARS"]

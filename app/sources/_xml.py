"""Tiny XML helpers shared by every sitemap/RSS method-family module.

Uses stdlib `xml.etree.ElementTree` rather than `lxml` (not an installed
dependency — see `app/sources/_util.py`'s docstring) or regex-based
extraction (speed-techniques.md §3#8's "skip DOM for sitemap XML"
suggestion, which this deliberately does NOT follow — see the note in
`app/sources/_sitemap.py`). `ElementTree.fromstring` does not resolve
external entities by default (no DTD/XInclude processing), so this is
not XXE-exposed the way `lxml.etree.parse(resolve_entities=True)` would
be — safe to point at third-party-controlled sitemap/RSS documents.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET


def local_tag(tag: str) -> str:
    """Strip a `{namespace-uri}` prefix so callers can match on the bare
    tag name (`loc`, `title`, `publication_date`, ...) regardless of
    which XML namespace (or none) the document declares — Google
    News sitemaps use `news:title`/`news:publication_date`, plain
    sitemaps don't namespace anything, RSS uses no namespace on `item`
    but sometimes namespaces extensions.
    """
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_xml(content: bytes | str) -> ET.Element | None:
    """Parse XML content, returning `None` (never raising) on malformed
    input — one bad document must not abort a whole source's discovery.

    Retries ONCE with anything after the root element's closing tag
    removed. That is not a general "repair broken XML" pass: it is the
    one specific corruption a CDN inflicts on an otherwise-valid feed,
    and it was silently costing a whole source.

    Shomoyer Alo's `/rss` is served through Cloudflare, which appends
    its `__CF$cv$params` beacon AFTER `</rss>`:

        </channel>
        </rss><script>(function(){...})();</script>

    XML has exactly one root element, so `ET.fromstring` rejects the
    whole document with "junk after document element" — every one of
    the 15 live, correctly-dated items in front of the injected script
    was thrown away. The source reported `status="empty"`, which reads
    as a quiet news day rather than a parse failure, so it went
    unnoticed (run 125, "Shomoyer Alo: 0 fetched" after 2.38s against a
    feed that returned HTTP 200 and 37 KB).

    Only content BEFORE the root's close is kept, so this can never
    invent an element the feed did not send: the retry either finds the
    real closing tag and parses the real document, or finds nothing and
    returns `None` exactly as before.
    """
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        pass
    trimmed = _trim_after_root_close(content)
    if trimmed is None:
        return None
    try:
        return ET.fromstring(trimmed)
    except ET.ParseError:
        return None


# The first tag-open that is not a processing instruction (`<?xml`),
# a doctype or a comment (`<!...`) — i.e. the root element's name.
_ROOT_TAG_RE = re.compile(rb"<\s*(?![?!/])([A-Za-z_][\w.:-]*)")


def _trim_after_root_close(content: bytes | str) -> bytes | None:
    """Everything up to and including the LAST `</root>`, where `root`
    is the document's first element name. `None` when there is no root
    element, no matching closing tag, or nothing after it (nothing to
    trim, so nothing a retry could fix).
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    match = _ROOT_TAG_RE.search(raw)
    if match is None:
        return None
    close = b"</" + match.group(1) + b">"
    end = raw.rfind(close)
    if end == -1:
        return None
    tail = raw[end + len(close) :]
    if not tail.strip():
        return None  # nothing after the close — the failure is elsewhere
    return raw[: end + len(close)]


def first_child_text(element: ET.Element, *tag_names: str) -> str | None:
    """First descendant (anywhere in the subtree, matching BeautifulSoup's
    `.find()` semantics that the legacy code relied on) whose local tag
    name is one of `tag_names`, checked in priority order. Returns
    stripped text, or `None` if none match / the match has no text.
    """
    by_tag: dict[str, ET.Element] = {}
    for el in element.iter():
        lt = local_tag(el.tag)
        if lt in tag_names and lt not in by_tag:
            by_tag[lt] = el
    for name in tag_names:
        found = by_tag.get(name)
        if found is not None and found.text:
            return found.text.strip()
    return None

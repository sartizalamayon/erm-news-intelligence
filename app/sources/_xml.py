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
    """
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        return None


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

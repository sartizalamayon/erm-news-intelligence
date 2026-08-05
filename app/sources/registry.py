"""Source registry — the single wiring point named in
docs/speed-techniques.md §4 point 1 ("A shared registry.py is the only
wiring point"). Nothing outside this file and app/batch should import a
concrete source module directly.

Intentionally empty: no source has been ported from `backend/` yet.
That is P1-FETCH's job — cherry-picking proven per-site logic out of
`backend/collector_logic.py` into one `app/sources/<site>.py` per
outlet, each decorated with `@register` below.
"""

from __future__ import annotations

from app.sources.base import NewsSource

_REGISTRY: dict[str, type[NewsSource]] = {}


def register(source_cls: type[NewsSource]) -> type[NewsSource]:
    """Class decorator: `@register` on a NewsSource subclass adds it to
    the registry, keyed by its `key` attribute.
    """
    _REGISTRY[source_cls.key] = source_cls
    return source_cls


def all_sources() -> dict[str, type[NewsSource]]:
    """Return a shallow copy of the registry (source_key -> class)."""
    return dict(_REGISTRY)

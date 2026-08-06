"""Source registry — the single wiring point named in
docs/speed-techniques.md §4 point 1 ("A shared registry.py is the only
wiring point"). Nothing outside this file and app/batch should import a
concrete source module directly.

`_SOURCE_MODULES` is the literal instantiation list (mirroring Repo B's
`registry.py:67-152`, speed-techniques.md §2): adding a source touches
exactly two things — the new `app/sources/<site>.py` file, and one line
here. Nothing else changes; the collector, the batch entrypoint, and
every other source stay untouched.

Ported (21 of 51 legacy sources, ranked by article volume — see the
P1-FETCH task report for the full coverage table and why the other 30
were left for a follow-up rather than ported shallowly):
Daily Inqilab, Naya Diganta, Prothom Alo, Kalbela, BBC News, BD Pratidin,
Dhaka Post, Desh Rupantor, Daily Star, The Business Standard, Dhaka Mail,
New Age, Daily Sun, Rupali BD, Nagorik, NDTV, Daily Adin, Sangbad,
The Financial Express, CNN, Al Jazeera.
"""

from __future__ import annotations

import importlib

from app.sources.base import NewsSource

_REGISTRY: dict[str, type[NewsSource]] = {}

_SOURCE_MODULES: tuple[str, ...] = (
    "app.sources.daily_inqilab",
    "app.sources.naya_diganta",
    "app.sources.prothom_alo",
    "app.sources.kalbela",
    "app.sources.bbc_news",
    "app.sources.bd_pratidin",
    "app.sources.dhaka_post",
    "app.sources.desh_rupantor",
    "app.sources.daily_star",
    "app.sources.business_standard",
    "app.sources.dhaka_mail",
    "app.sources.new_age",
    "app.sources.daily_sun",
    "app.sources.rupali_bd",
    "app.sources.nagorik",
    "app.sources.ndtv",
    "app.sources.daily_adin",
    "app.sources.sangbad",
    "app.sources.financial_express",
    "app.sources.cnn",
    "app.sources.al_jazeera",
)


def register(source_cls: type[NewsSource]) -> type[NewsSource]:
    """Class decorator: `@register` on a NewsSource subclass adds it to
    the registry, keyed by its `key` attribute.
    """
    _REGISTRY[source_cls.key] = source_cls
    return source_cls


def load_all() -> None:
    """Import every concrete source module so its `@register` decorator
    runs. Idempotent (re-importing an already-imported module is a
    no-op) — safe to call more than once (tests, batch entrypoint).
    Call this before `all_sources()` if you need the FULL registered
    set; a test that only cares about a specific source can import that
    module directly instead.
    """
    for module_name in _SOURCE_MODULES:
        importlib.import_module(module_name)


def all_sources() -> dict[str, type[NewsSource]]:
    """Return a shallow copy of the registry (source_key -> class)."""
    return dict(_REGISTRY)

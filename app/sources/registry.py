"""Source registry — the single wiring point named in
docs/speed-techniques.md §4 point 1 ("A shared registry.py is the only
wiring point"). Nothing outside this file and app/batch should import a
concrete source module directly.

`_SOURCE_MODULES` is the literal instantiation list (mirroring Repo B's
`registry.py:67-152`, speed-techniques.md §2): adding a source touches
exactly two things — the new `app/sources/<site>.py` file, and one line
here. Nothing else changes; the collector, the batch entrypoint, and
every other source stay untouched.

Ported (45 of 51 legacy sources, 98.06% of historical article volume,
ranked by article volume — see the P1-FETCH task report for the full
coverage table and why the remaining 6 were skipped rather than ported
shallowly):
Daily Inqilab, Naya Diganta, Prothom Alo, Kalbela, BBC News, BD Pratidin,
Dhaka Post, Desh Rupantor, Daily Star, The Business Standard, Dhaka Mail,
New Age, Daily Sun, Rupali BD, Nagorik, NDTV, Daily Adin, Sangbad,
The Financial Express, CNN, Al Jazeera, Dhaka Tribune, BD News 24,
Daily Sangram, Independent TV, Somoy Journal, Channel i Online,
Arthosuchak, Bangladesh Post, Prothom Alo English, Ekattor TV,
Ajker Patrika, Channels, Deshersangbad, News24 BD, Shomoyer Alo,
Times of Bangladesh, Bonik Barta, Desh Protikhon, Economy Post,
TechWorld, Daily ICT News, DigiBangla, Tech Shohor, Bangla Tech 24.
"""

from __future__ import annotations

import importlib

from app.sources.base import NewsSource

_REGISTRY: dict[str, type[NewsSource]] = {}

_SOURCE_MODULES: tuple[str, ...] = (
    # ── Original 21 (top by volume, 82.0%) ──────────────────────────
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
    # ── Tail sources added in the follow-up pass (+16.06pp, -> 98.06%) ──
    "app.sources.dhaka_tribune",
    "app.sources.bd_news_24",
    "app.sources.daily_sangram",
    "app.sources.independent_tv",
    "app.sources.somoy_journal",
    "app.sources.channel_i_online",
    "app.sources.arthosuchak",
    "app.sources.bangladesh_post",
    "app.sources.prothom_alo_english",
    "app.sources.ekattor_tv",
    "app.sources.ajker_patrika",
    "app.sources.channels",
    "app.sources.deshersangbad",
    "app.sources.news24_bd",
    "app.sources.shomoyer_alo",
    "app.sources.times_of_bangladesh",
    "app.sources.bonik_barta",
    "app.sources.desh_protikhon",
    "app.sources.economy_post",
    "app.sources.techworld",
    "app.sources.daily_ict_news",
    "app.sources.digibangla",
    "app.sources.tech_shohor",
    "app.sources.bangla_tech_24",
    # ── The last 6: enabled since the rebuild but never collected ───
    # Each was seeded in the `sources` table with a `method` name but no
    # plugin behind it, so `registry_key` stayed NULL. Collection
    # iterates THIS list and uses the table only to veto, so all six
    # were skipped on every run while showing as healthy. Each file's
    # docstring records what its site actually exposes, measured live.
    "app.sources.amar_barta",
    "app.sources.bss",
    "app.sources.deshpatrika",
    "app.sources.infotech_insight",
    "app.sources.share_biz",
    "app.sources.share_business_24",
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

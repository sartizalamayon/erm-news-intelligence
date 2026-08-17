"""app.sources.registry — the single wiring point. Verifies all 51
ported sources register cleanly, satisfy the `NewsSource` ABC contract,
and have the metadata `app/services/collector.py` depends on
(`key`, `display_name`). Importing every module here is itself a
smoke test: a source file with a syntax error or a broken top-level
config would fail at `load_all()`, not silently.
"""

from __future__ import annotations

from app.sources.base import NewsSource
from app.sources.registry import _SOURCE_MODULES, all_sources, load_all

EXPECTED_KEYS = {
    # Original 21
    "daily_inqilab",
    "naya_diganta",
    "prothom_alo",
    "kalbela",
    "bbc_news",
    "bd_pratidin",
    "dhaka_post",
    "desh_rupantor",
    "daily_star",
    "business_standard",
    "dhaka_mail",
    "new_age",
    "daily_sun",
    "rupali_bd",
    "nagorik",
    "ndtv",
    "daily_adin",
    "sangbad",
    "financial_express",
    "cnn",
    "al_jazeera",
    # Tail follow-up pass (+24)
    "dhaka_tribune",
    "bd_news_24",
    "daily_sangram",
    "independent_tv",
    "somoy_journal",
    "channel_i_online",
    "arthosuchak",
    "bangladesh_post",
    "prothom_alo_english",
    "ekattor_tv",
    "ajker_patrika",
    "channels",
    "deshersangbad",
    "news24_bd",
    "shomoyer_alo",
    "times_of_bangladesh",
    "bonik_barta",
    "desh_protikhon",
    "economy_post",
    "techworld",
    "daily_ict_news",
    "digibangla",
    "tech_shohor",
    "bangla_tech_24",
    # The last 6. Enabled in the `sources` table since the rebuild but
    # never collected: each had a `method` name and no plugin, so
    # `registry_key` stayed NULL and collection (which iterates THIS
    # registry and uses the table only to veto) skipped them on every
    # run while they showed as an ordinary quiet day.
    "amar_barta",
    "bss",
    "deshpatrika",
    "infotech_insight",
    "share_biz",
    "share_business_24",
}


def test_load_all_registers_every_expected_source():
    load_all()
    sources = all_sources()
    assert set(sources) == EXPECTED_KEYS
    assert len(sources) == 51


def test_source_module_count_matches_registry():
    assert len(_SOURCE_MODULES) == 51


def test_every_registered_source_is_a_news_source_subclass():
    load_all()
    for key, cls in all_sources().items():
        assert issubclass(cls, NewsSource), key
        assert cls.key == key


def test_every_registered_source_has_a_display_name():
    load_all()
    for key, cls in all_sources().items():
        name = getattr(cls, "display_name", None)
        assert isinstance(name, str) and name, f"{key} missing display_name"


def test_every_registered_source_implements_both_abc_methods():
    load_all()
    for key, cls in all_sources().items():
        # abstractmethods are satisfied (class is instantiable) — a
        # source missing either iter_feed_items or fetch_article would
        # fail at class-definition time already (ABCMeta), but this
        # additionally proves neither method is left as the ABC's own
        # NotImplementedError-raising stub.
        assert cls.iter_feed_items is not NewsSource.iter_feed_items, key
        assert cls.fetch_article is not NewsSource.fetch_article, key


def test_registry_keys_are_unique_and_match_module_list():
    """Every module in _SOURCE_MODULES registers exactly one class, and
    no two modules silently collide on the same `key` (which would make
    one source invisible — the later @register call overwrites the dict
    entry).
    """
    load_all()
    assert len(all_sources()) == len(_SOURCE_MODULES)


def test_all_sources_returns_a_copy_not_the_live_registry():
    load_all()
    snapshot = all_sources()
    snapshot["fake"] = object()  # type: ignore[assignment]
    assert "fake" not in all_sources()

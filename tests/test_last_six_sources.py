"""The six sources that were enabled but could never fetch.

Each was seeded in `sources` with a `method` name and no plugin behind
it, so `registry_key` stayed NULL. Collection iterates the CODE registry
and uses the table only to veto, so all six were skipped by every run
while the health screen showed them as an ordinary quiet day.

No network here: the Amarbarta parser is exercised against a captured
fragment, and the rest is registration wiring.
"""

from __future__ import annotations

from app.sources.amar_barta import parse_archive_fragment
from app.sources.registry import all_sources, load_all

THE_SIX = (
    "amar_barta",
    "bss",
    "deshpatrika",
    "infotech_insight",
    "share_biz",
    "share_business_24",
)

# Two cards, trimmed from a real response on 2026-08-17. The shape that
# matters: each article contributes TWO anchors (thumbnail, then
# headline) with the section link and the timestamp in between.
FRAGMENT = (
    '<div class="col-12 col-md-3 border-right"><div class="common-lead-content position-relative">'
    '<a href="https://www.amarbarta.com/national/83670">'
    '<div class="image-lead"><span class="imgWrep">'
    '<img class="images" src="/assets/x.jpg" title="" alt="" /></span></div>'
    "</a>"
    '<a href="/national"><div>জাতীয়</div></a>'
    '<div style="color: #666">২০২৬-০৮-১৭ ১৪:১২:১৫ ইং</div>'
    '<a href="https://www.amarbarta.com/national/83670">'
    '<h5 class="title">First headline</h5></a></div></div>'
    '<div class="col-12 col-md-3 border-right"><div class="common-lead-content position-relative">'
    '<a href="https://www.amarbarta.com/sports/83669">'
    '<div class="image-lead"><img src="/assets/y.jpg" title="" /></div></a>'
    '<a href="/sports"><div>খেলা</div></a>'
    '<div style="color: #666">২০২৬-০৮-১৭ ১৪:০৮:৫৪ ইং</div>'
    '<a href="https://www.amarbarta.com/sports/83669">'
    '<h5 class="title">Second headline</h5></a></div></div>'
)


def test_amarbarta_parses_url_title_and_bengali_timestamp() -> None:
    items = parse_archive_fragment(FRAGMENT, source="amar_barta")

    assert len(items) == 2
    first, second = items

    assert first.url == "https://www.amarbarta.com/national/83670"
    assert first.source_id == "83670"
    assert first.title == "First headline"
    assert first.published_at is not None
    # Bengali digits, ISO order, Dhaka local time.
    assert (first.published_at.year, first.published_at.month, first.published_at.day) == (2026, 8, 17)
    assert (first.published_at.hour, first.published_at.minute) == (14, 12)

    assert second.title == "Second headline"
    assert second.source_id == "83669"


def test_amarbarta_reads_the_headline_not_the_empty_image_title() -> None:
    """The first version of this parser bounded each block on the NEXT
    ANCHOR. Every card carries two anchors, so the block ended before
    the <h5> and the only `title=` left in range was the thumbnail's
    empty one: 20 correctly dated items with 20 blank headlines.
    """
    items = parse_archive_fragment(FRAGMENT, source="amar_barta")
    assert all(i.title for i in items), "a card's headline must survive its second anchor"


def test_amarbarta_keeps_an_undated_card_rather_than_dropping_it() -> None:
    """Matching every other family: an undated article is filtered later
    by `is_recent`'s three-valued check, not discarded at parse time.
    """
    undated = (
        '<div class="col-12 col-md-3"><a href="https://www.amarbarta.com/national/1">'
        '<h5 class="title">No date here</h5></a></div>'
    )
    items = parse_archive_fragment(undated, source="amar_barta")
    assert len(items) == 1
    assert items[0].published_at is None
    assert items[0].title == "No date here"


def test_amarbarta_returns_nothing_for_an_unrecognised_fragment() -> None:
    """A template change must degrade to "found nothing", which the
    caller turns into a gnews fallback, rather than raising.
    """
    assert parse_archive_fragment("<html><body>not the archive</body></html>", source="amar_barta") == []


def test_all_six_are_registered_and_importable() -> None:
    load_all()
    registry = all_sources()
    missing = [k for k in THE_SIX if k not in registry]
    assert missing == [], f"not registered, so collection still skips them: {missing}"


def test_each_of_the_six_declares_a_display_name_matching_its_db_row() -> None:
    """`registry_key` links the plugin to its row, but the health screen
    and the run panel both key their per-source lines off display_name,
    so a mismatch shows the source twice: once fetching, once idle.
    """
    load_all()
    registry = all_sources()
    expected = {
        "amar_barta": "Amarbarta",
        "bss": "BSS",
        "deshpatrika": "Deshpatrika",
        "infotech_insight": "InfoTech",
        "share_biz": "ShareBiz",
        "share_business_24": "ShareBusiness24",
    }
    for key, display in expected.items():
        assert registry[key].display_name == display

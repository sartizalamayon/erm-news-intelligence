"""Precision tests for app.db.econaxes -- the six economy axes.

Every assertion here is a headline that really appears in the corpus
(July-August 2026), and every NEGATIVE assertion is a false positive
that was actually observed and then fixed. That is the point of the
file: `app/db/watchlist.py`'s docstring argues that a dashboard number
has nobody downstream to discard false positives, so the guards are
only worth anything if something keeps them honest when the patterns
are next edited.

Postgres-gated (pg_available()), matching every other DB-backed test in
this repo -- and it has to be a real server rather than Python's `re`,
because these patterns depend on Postgres POSIX semantics: `\\y` word
boundaries, the case-sensitive `~` operator against lower()'d text, and
the exact Bangla boundary behaviour watchlist.py's docstring warns
about.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.db.econaxes import (
    ECON_AXES,
    AxisRule,
    EconAxis,
    axis_by_key,
    axis_where_sql,
    econ_prefilter_pattern,
    econ_text_sql,
    normalized_text_sql,
)
from app.db.testing import migrated_schema, pg_available

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


@pytest.fixture(scope="module")
def conn() -> Iterator[object]:
    """One connection for the whole module. These tests never write --
    they evaluate the axis predicates against literal headlines through
    a VALUES row -- so a single shared schema is safe and saves running
    the migration stack once per test.
    """
    with migrated_schema() as c:
        yield c


def matches(conn: object, axis_key: str, title: str, title_en: str = "") -> bool:
    """True if `axis_key` would claim this headline.

    Evaluates the real `axis_where_sql` output against a one-row VALUES
    aliased as `t(title, title_en)` -- the same two columns
    `normalized_text_sql` reads -- so the test exercises the shipped SQL
    rather than a reimplementation of it.
    """
    axis = axis_by_key(axis_key)
    assert axis is not None, f"no such axis: {axis_key}"
    where, params = axis_where_sql(axis, table_alias="t")
    # psycopg binds by position in the SQL TEXT, and `where` is spliced
    # into the SELECT list ahead of the VALUES row -- so its params come
    # first.
    row = conn.execute(  # type: ignore[attr-defined]
        f"SELECT {where} AS m FROM (VALUES (%s, %s)) AS t(title, title_en)",  # noqa: S608
        [*params, title, title_en],
    ).fetchone()
    return bool(row["m"])


def which_axes(conn: object, title: str, title_en: str = "") -> set[str]:
    return {a.key for a in ECON_AXES if matches(conn, a.key, title, title_en)}


# ── contract ────────────────────────────────────────────────────────


def test_the_six_axes_are_present_and_uniquely_keyed() -> None:
    assert len(ECON_AXES) == 6
    keys = [a.key for a in ECON_AXES]
    assert keys == ["energy", "trade_rmg", "fx", "banking", "revenue_tax", "capital_markets"]
    assert len(set(keys)) == len(keys)
    for axis in ECON_AXES:
        assert axis.label and axis.note and axis.rules


def test_axis_where_sql_returns_one_param_per_pattern_and_context() -> None:
    """The param list must line up with the placeholders in the SQL, or
    a caller composing several axes into one statement silently binds
    the wrong pattern to the wrong axis.
    """
    for axis in ECON_AXES:
        where, params = axis_where_sql(axis)
        expected = sum(1 for r in axis.rules if r.context) + len(axis.rules)
        assert where.count("%s") == len(params) == expected, axis.key


def test_axis_where_sql_retargets_the_table_alias() -> None:
    where, _ = axis_where_sql(ECON_AXES[0], table_alias="hits")
    assert "hits.title" in where
    assert "a.title" not in where


def test_econ_text_sql_composes_on_the_watchlist_constant_not_a_copy() -> None:
    """The strip layer must WRAP watchlist's normalized text, so the
    অন্তরঙ্গ removal that module documents still applies here.
    """
    assert normalized_text_sql("a") in econ_text_sql("a")
    assert "অন্তরঙ্গ" in econ_text_sql("a")
    assert "regexp_replace" in econ_text_sql("a")


def test_prefilter_is_a_strict_superset_of_every_axis(conn: object) -> None:
    """The rollup's performance guard must never drop a row an axis
    would have claimed.
    """
    pattern = econ_prefilter_pattern()
    for title, title_en in _EVERY_POSITIVE_HEADLINE:
        row = conn.execute(  # type: ignore[attr-defined]
            f"SELECT {normalized_text_sql('t')} ~ %s AS m FROM (VALUES (%s, %s)) AS t(title, title_en)",  # noqa: S608
            [pattern, title, title_en],
        ).fetchone()
        assert row["m"], f"prefilter would have dropped: {title or title_en}"


# ── energy ──────────────────────────────────────────────────────────


def test_energy_claims_real_power_and_gas_stories(conn: object) -> None:
    assert matches(conn, "energy", "", "LNG terminal glitch cuts gas supply by 450 mmcfd nationwide")
    assert matches(conn, "energy", "", "Dhaka joins load-shedding to calm rural anger")
    assert matches(conn, "energy", "কুড়িগ্রামে সপ্তাহব্যাপী লোডশেডিং")
    assert matches(conn, "energy", "", "Govt plans FSRU in Mongla to strengthen fuel security")
    assert matches(conn, "energy", "অবিলম্বে গ্যাস ও বিদ্যুৎ সংকট নিরসনের দাবি")


def test_energy_excludes_electrocution_deaths(conn: object) -> None:
    """KNOWN BAD MATCH. বিদ্যুৎ ("electricity") is a substring of
    বিদ্যুৎস্পৃষ্ট ("electrocuted"), so before ECON_STRIP_RE these
    crime-and-accident stories were 89 of the axis's 648 matches -- and
    not one of the 89 had ever been scored, which is what gave them
    away. Same class of trap as watchlist.py's তরঙ্গ ⊂ অন্তরঙ্গ.
    """
    assert not matches(conn, "energy", "বোয়ালখালীতে বিদ্যুৎস্পৃষ্ট হয়ে যুবকের মৃত্যু")
    assert not matches(conn, "energy", "চাঁপাইনবাবগঞ্জে বিদ্যুৎস্পৃষ্ট হয়ে ৩ জনের মৃত্যু")
    assert not matches(conn, "energy", "পাথরঘাটায় বিদ্যুৎপৃষ্টে নির্মাণ শ্রমিকের মৃত্যু")
    assert not matches(conn, "energy", "মাগুরায় বিদ্যুৎস্পর্শে যুবকের মৃত্যু")
    assert not matches(conn, "energy", "ট্রান্সফরমার চুরি করতে গিয়ে বিদ্যুতায়িত চোর")


def test_the_strip_does_not_swallow_a_real_mention_in_the_same_headline(conn: object) -> None:
    """Removing the electrocution compound must leave any OTHER
    বিদ্যুৎ in the headline still matching -- the strip is surgical,
    not a whole-row veto.
    """
    assert matches(conn, "energy", "বিদ্যুৎস্পৃষ্ট হয়ে মৃত্যু, এলাকায় বিদ্যুৎ সংকট চরমে")


# ── trade_rmg ───────────────────────────────────────────────────────


def test_trade_rmg_claims_real_apparel_and_trade_stories(conn: object) -> None:
    assert matches(conn, "trade_rmg", "", "Bangladesh slips to third in global garment exports")
    assert matches(conn, "trade_rmg", "", "Textile mills want refinancing scheme, RMG uninterrupted gas")
    assert matches(conn, "trade_rmg", "", "Lower US tariff boosts Bangladesh's RMG edge")
    assert matches(conn, "trade_rmg", "পোশাক রপ্তানিতে দ্বিতীয় বাংলাদেশ")
    assert matches(conn, "trade_rmg", "পোশাকশ্রমিকদের মজুরি বাড়াতে শ্রমমন্ত্রীকে চিঠি দিয়েছে আইবিসি")


def test_trade_rmg_excludes_electricity_tariff_stories(conn: object) -> None:
    """KNOWN BAD MATCH, and the one the brief calls out: a bare
    `\\ytariffs?\\y` pulled ELECTRICITY tariff coverage onto the export
    line. Those are an energy operating cost, and the energy axis is
    where they belong.
    """
    headline = "Bangladesh Electricity Price Hike | Consumers stare at higher power bills | Electricity Tariff Hike"
    assert not matches(conn, "trade_rmg", "", headline)
    assert matches(conn, "energy", "", headline)
    assert not matches(conn, "trade_rmg", "", "VAT to be cut from electricity bills in October")


def test_trade_rmg_excludes_clothing_in_its_ordinary_sense(conn: object) -> None:
    """KNOWN BAD MATCH. পোশাক is simply the Bangla word for clothing.
    Unguarded it collected Bollywood fashion, wedding runways, an
    orangutan in a jumper, and -- via সাদাপোশাক ("plainclothes") -- an
    abduction story.
    """
    assert not matches(conn, "trade_rmg", "রাঙামাটি থেকে সাদাপোশাকে তুলে নেওয়া সাংবাদিকের জামিন হয়নি")
    assert not matches(conn, "trade_rmg", "ইস্তিরি ছাড়াই পরা যাবে এসব পোশাক, দেখে নিন ৭টি ছবি")
    assert not matches(conn, "trade_rmg", "ঠান্ডা থেকে বাঁচতে গরম পোশাকে ওরাংওটাং")
    assert not matches(conn, "trade_rmg", "বিয়ের পোশাকের ফ্যাশন শো, ছিলেন বলিউডের তারকারাও")
    assert not matches(conn, "trade_rmg", "অফ-শোল্ডার পোশাকে এক ‘অন্যরকম’ দীঘি")


# ── fx ──────────────────────────────────────────────────────────────


def test_fx_claims_real_currency_and_reserve_stories(conn: object) -> None:
    assert matches(conn, "fx", "", "Gross forex reserves rise to $37.11b")
    assert matches(conn, "fx", "", "Country's reserves rise")
    assert matches(conn, "fx", "দেশের বৈদেশিক মুদ্রার রিজার্ভ ৩৬ দশমিক ৫৯৩ বিলিয়ন ডলার")
    assert matches(conn, "fx", "", "Reserve sees surprise increase as dollar rate declines")
    assert matches(conn, "fx", "দেশের বাজারে ডলারের দাম কমল")


def test_fx_excludes_nature_reserves(conn: object) -> None:
    """KNOWN BAD MATCH. `\\yreserves?\\y` is the axis's best token and
    also matches wildlife: 12 of its 51 raw matches were nature, tiger
    and forest reserves, scoring 1-2 where they were scored at all.
    """
    assert not matches(conn, "fx", "", "How Benfield Hill Nature Reserve helps glow worm population")
    assert not matches(conn, "fx", "", "Injured Tigress Found In Pilibhit Tiger Reserve Dies During Treatment")
    assert not matches(conn, "fx", "", "First confirmed breeding Bittern at Dorset nature reserve")
    assert not matches(conn, "fx", "হবিগঞ্জের রিজার্ভ ফরেস্ট থেকে ৬টি পিস্তল উদ্ধার")


def test_fx_excludes_commodity_stockpiles(conn: object) -> None:
    """KNOWN BAD MATCH. An oil or food reserve is a stockpile, not a
    currency holding -- and the oil ones score 9, so leaving them in
    reported an energy shock as an FX shock.
    """
    assert not matches(conn, "fx", "", "Bangladesh targets 34 lakh tonnes food reserve capacity")
    assert not matches(conn, "fx", "", "Govt plans strategic energy reserve system")
    assert not matches(conn, "fx", "", "Does riding a motorcycle in reserve fuel harm the engine?")


def test_fx_excludes_stories_that_merely_quote_a_dollar_figure(conn: object) -> None:
    """KNOWN BAD MATCH, and the largest single correction in this file.
    ডলার is the unit any story quotes a big number in: 207 of its 251
    raw matches carried no FX cue at all. Worst of them were the
    oil-price headlines, which score 9-10 and moved 31 critical ENERGY
    articles onto the FX line.
    """
    assert not matches(conn, "fx", "", "The Odyssey earns $258 million in its first week")
    assert not matches(conn, "fx", "", "FIFA has earned $15 billion from the World Cup")
    assert not matches(conn, "fx", "", "Elon Musk's wealth drops by $45 billion in a day")
    assert not matches(conn, "fx", "উৎসবের রাতে জাদুঘরের ৯ কোটি ডলারের ৪ মাস্টারপিস চুরি")
    oil = "মধ্যপ্রাচ্যের উত্তেজনায় ৯২ ডলারে পৌঁছাল অপরিশোধিত তেলের দাম"
    assert not matches(conn, "fx", oil)


# ── banking ─────────────────────────────────────────────────────────


def test_banking_claims_real_sector_stories(conn: object) -> None:
    assert matches(conn, "banking", "", "Bangladesh Bank cuts policy interest rates")
    assert matches(conn, "banking", "", "29 banks given ultimatum to cut NPLs")
    assert matches(conn, "banking", "একীভূত পাঁচ ব্যাংকই বুঝে পেল সম্মিলিত ইসলামী ব্যাংক")
    assert matches(conn, "banking", "ফ্রিল্যান্সিং খাতে উৎসাহ দিতে নতুন নির্দেশনা কেন্দ্রীয় ব্যাংকের")
    # Real rows from Bangladeshi sources carry the Bangla headline too,
    # which is what identifies this one -- the English translation alone
    # says only "banks" and "non-performing loans".
    assert matches(
        conn, "banking",
        "খেলাপি ঋণ কমাতে ২৯ ব্যাংকের ওপর ছয় মাসের আল্টিমেটাম",
        "29 banks given six-month ultimatum to reduce non-performing loans",
    )


def test_banking_declines_the_bangkok_homograph_in_both_directions(conn: object) -> None:
    """The measured cost of the Bangkok guard, pinned so nobody
    "fixes" it by loosening the boundary.

    ব্যাংককে is a true homograph: it is both the dative of ব্যাংক ("to
    the bank") and the locative of ব্যাংকক ("in Bangkok"). Identical
    spelling, so no pattern can keep one and drop the other. `\\y` drops
    both, which costs the axis about 20 headlines in a two-month window
    against 802 kept -- the precision-over-recall bias watchlist.py
    commits to, paid in a place worth recording.
    """
    assert not matches(conn, "banking", "১০০ শতাংশ ক্যাশ মার্জিনে রূপালী ব্যাংককে এলসি খোলার অনুমতি")
    assert not matches(conn, "banking", "ব্যাংকগুলোর মোট ঋণের ৪ শতাংশ দিতে হবে কৃষি খাতে")


def test_banking_excludes_the_west_bank(conn: object) -> None:
    """KNOWN BAD MATCH, and the reason the axis names its banks rather
    than matching a bare `\\ybanks?\\y`. That token would have added 486
    headlines: English-language bank marketing (card launches, branch
    anniversaries, vehicle discounts, an Indian apprentice result) and,
    worse, the West Bank -- Israeli-Palestinian coverage scoring up to
    9, which would have read as a Bangladeshi banking emergency.
    """
    assert not matches(conn, "banking", "", "Israeli settlers set fire to West Bank mosques after deadly clash")
    assert not matches(conn, "banking", "", "Israel's 'Crimson Thread' military barrier is strangling the West Bank")
    assert not matches(
        conn, "banking", "", "Fear and attacks grip Taybeh, the last all-Christian town in the West Bank",
    )


def test_banking_excludes_bangkok(conn: object) -> None:
    """KNOWN BAD MATCH -- the one that would have been easy to miss.
    watchlist.py warns that `\\y` does not behave for Bangla, so
    `\\yব্যাংক\\y` looks like it should match ব্যাংকক (Bangkok). Measured,
    it does not: the boundary falls where it needs to. This test pins
    that behaviour, because the axis silently acquires Thai travel news
    the day it stops holding.
    """
    assert not matches(conn, "banking", "ব্যাংকক সফর সংক্ষিপ্ত করে শুক্রবার দেশে ফিরছেন স্পিকার")
    assert not matches(conn, "banking", "ব্যাংককের উদ্দেশে ঢাকা ছাড়লেন আইজিপি")
    assert not matches(conn, "banking", "ব্যাংককে জমে উঠেছে অদ্ভুত ‘পেট এক্সপো ২০২৬’")


def test_banking_still_matches_inflected_bangla_forms(conn: object) -> None:
    """The flip side of the Bangkok guard: the ordinary case endings
    must still match, or the axis loses most of its Bangla coverage.
    """
    assert matches(conn, "banking", "ডাচ-বাংলা ব্যাংকের দ্বিতীয় প্রান্তিকে মুনাফা বেড়েছে")
    assert matches(conn, "banking", "ব্যাংকিং খাতের সংস্কার নিয়ে আইএমএফের উদ্বেগ")


# ── revenue_tax ─────────────────────────────────────────────────────


def test_revenue_tax_claims_real_fiscal_stories(conn: object) -> None:
    assert matches(conn, "revenue_tax", "", "NBR opens online income tax return filing for FY27")
    assert matches(conn, "revenue_tax", "", "45 percent growth needed to achieve revenue target: NBR Chairman")
    assert matches(conn, "revenue_tax", "২০২৫-২৬ অর্থবছরে রাজস্ব আদায়ে ঘাটতি বাড়ছে")
    assert matches(conn, "revenue_tax", "", "IMF wants specific roadmap for bank sector reforms and single VAT rate")


def test_revenue_tax_excludes_the_vatican(conn: object) -> None:
    """KNOWN BAD MATCH. ভ্যাট ("VAT") is a substring of ভ্যাটিকান
    ("Vatican") -- the same Bangla trap as বিদ্যুৎ and পোশাক above.
    """
    headline = "নোবেলজয়ীদের সঙ্গে এআই ও পারমাণবিক অস্ত্র নিয়ে ভ্যাটিকান ঘোষণাপত্রে সই করলেন অধ্যাপক ইউনূস"
    assert not matches(conn, "revenue_tax", headline)


def test_revenue_tax_excludes_other_countries_public_finances(conn: object) -> None:
    """KNOWN BAD MATCH. `budget` and `vat` are generic fiscal words and
    without the Bangladesh guard the axis reported the Pentagon, the UK
    Treasury and Indonesia's food programme as Bangladeshi tax risk --
    including two US war-funding stories scoring 8 and 9.
    """
    assert not matches(conn, "revenue_tax", "", "US House Approves $95 Billion Budget Plan With Iran War Funding")
    assert not matches(conn, "revenue_tax", "", "Pentagon Lost Its Way Amid War Costs in Iran")
    assert not matches(
        conn, "revenue_tax", "", "UK borrows more than expected in July as Healey prepares for first Budget",
    )
    assert not matches(conn, "revenue_tax", "", "Indonesia cuts Free Food Program budget by more than $2 billion")
    assert not matches(conn, "revenue_tax", "", "Electricity bills: VAT cut will not apply in NI")


def test_revenue_tax_excludes_the_consumer_budget_sense(conn: object) -> None:
    """KNOWN BAD MATCH: "budget" as in cheap."""
    assert not matches(conn, "revenue_tax", "", "Phones are too expensive. I tested 3 great budget phones")
    assert not matches(conn, "revenue_tax", "", "Affordable and Safe SUVs: Find the Top 5 Cars on a Budget")


# ── capital_markets ─────────────────────────────────────────────────


def test_capital_markets_claims_real_market_stories(conn: object) -> None:
    assert matches(conn, "capital_markets", "", "DSEX slips below 5,900 as investors book profits")
    assert matches(conn, "capital_markets", "", "BSEC proposes sweeping margin loan reforms")
    assert matches(conn, "capital_markets", "ডিএসইতে পিই রেশিও বেড়েছে দশমিক ৩২ শতাংশ")
    assert matches(conn, "capital_markets", "ব্যাংক ও আর্থিক খাতের দাপটে পুঁজিবাজারে সূচকের বড় উত্থান")


def test_capital_markets_excludes_indian_market_coverage(conn: object) -> None:
    """KNOWN BAD MATCH, and the second trap the brief calls out. A bare
    `stock market` collected the Sensex live-blogs. Measured, it would
    have added 25 headlines of which 5 score >= 8 -- on the US-Iran OIL
    story they are actually about -- taking this axis's critical count
    from 2 to 7 and drawing a Bangladeshi market emergency that never
    happened.
    """
    assert not matches(
        conn, "capital_markets", "",
        "Stock Market LIVE Updates, Sensex Today: Sensex Drops 650 Points As Oil Advances Amid US-Iran Tensions",
    )
    assert not matches(
        conn, "capital_markets", "",
        "Stock Market Highlights, Sensex Today: Sensex, Nifty Close Flat As RBI Keeps Repo Rate Steady",
    )
    assert not matches(conn, "capital_markets", "", "Korean stock market's wild swings hit investors")


# ── cross-axis ──────────────────────────────────────────────────────


def test_a_headline_may_sit_on_more_than_one_axis(conn: object) -> None:
    """Axes are not mutually exclusive and must not be forced to be --
    BSEC genuinely belongs to both banking and capital markets, and a
    reader comparing the two lines needs each to be complete on its own
    terms.
    """
    axes = which_axes(conn, "", "BSEC proposes sweeping margin loan reforms to tighten market discipline")
    assert {"banking", "capital_markets"} <= axes


def test_pure_noise_lands_on_no_axis_at_all(conn: object) -> None:
    for title in (
        "সাত ডাকাত গ্রেপ্তার, অস্ত্র–গুলির সঙ্গে পাওয়া গেল আইনশৃঙ্খলা বাহিনীর পোশাক",
        "বোয়ালখালীতে বিদ্যুৎস্পৃষ্ট হয়ে যুবকের মৃত্যু",
    ):
        assert which_axes(conn, title) == set(), title
    for title_en in (
        "Free trails across Lincolnshire nature reserves this summer",
        "The Odyssey earns $258 million in its first week",
        "ব্যাংকক সফর সংক্ষিপ্ত করে দেশে ফিরছেন স্পিকার",
    ):
        assert which_axes(conn, "", title_en) == set(), title_en


def test_a_context_guard_narrows_and_never_widens(conn: object) -> None:
    """Structural property of AxisRule: adding a context can only ever
    remove matches. If this inverts, every guard in the file is a
    no-op and the counts silently inflate.
    """
    bare = EconAxis("t", "t", "t", (AxisRule(r"পোশাক"),))
    guarded = EconAxis("t", "t", "t", (AxisRule(r"পোশাক", context=r"রপ্তানি"),))
    for title in ("পোশাক রপ্তানিতে দ্বিতীয় বাংলাদেশ", "বিয়ের পোশাকের ফ্যাশন শো"):
        w1, p1 = axis_where_sql(bare, table_alias="t")
        w2, p2 = axis_where_sql(guarded, table_alias="t")
        r = conn.execute(  # type: ignore[attr-defined]
            f"SELECT {w1} AS bare, {w2} AS guarded FROM (VALUES (%s, %s)) AS t(title, title_en)",  # noqa: S608
            [*p1, *p2, title, ""],
        ).fetchone()
        assert not (r["guarded"] and not r["bare"]), title


# Every headline that must survive the rollup prefilter -- one real
# positive per axis, reused by test_prefilter_is_a_strict_superset.
_EVERY_POSITIVE_HEADLINE = (
    ("", "LNG terminal glitch cuts gas supply by 450 mmcfd nationwide"),
    ("", "Bangladesh slips to third in global garment exports"),
    ("", "Gross forex reserves rise to $37.11b"),
    ("", "Bangladesh Bank cuts policy interest rates"),
    ("", "NBR opens online income tax return filing for FY27"),
    ("", "DSEX slips below 5,900 as investors book profits"),
    ("অবিলম্বে গ্যাস ও বিদ্যুৎ সংকট নিরসনের দাবি", ""),
    ("পোশাক রপ্তানিতে দ্বিতীয় বাংলাদেশ", ""),
    ("দেশের বৈদেশিক মুদ্রার রিজার্ভ ৩৬ দশমিক ৫৯৩ বিলিয়ন ডলার", ""),
    ("একীভূত পাঁচ ব্যাংকই বুঝে পেল সম্মিলিত ইসলামী ব্যাংক", ""),
    ("২০২৫-২৬ অর্থবছরে রাজস্ব আদায়ে ঘাটতি বাড়ছে", ""),
    ("ডিএসইতে পিই রেশিও বেড়েছে দশমিক ৩২ শতাংশ", ""),
)

"""Regression tests for app.db.rollups -- the DASHBOARD-ROLLUPS mission:
populating migrations/0007_rollups.sql's rollup_monthly_* tables from the
real `articles` table so app.api.routers.dashboard's /dashboard/trends
has real data instead of empty tables.

Postgres-gated (pg_available()), matching every other DB-backed test in
this repo (tests/test_storage.py, tests/test_reaper_stale_runs.py, ...).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.econaxes import ECON_AXES
from app.db.queries import insert_article
from app.db.rollups import backfill_all_days, compute_rollups_for_day
from app.db.testing import migrated_schema, pg_available
from app.db.watchlist import all_entities

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

DAY = dt.date(2026, 3, 15)
MONTH = dt.date(2026, 3, 1)


def _article(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "url": "https://example.com/x",
        "url_norm": "https://example.com/x",
        "source": "Example Source",
        "title": "Headline",
        "day": DAY,
        "review_status": "pending",
        "sections": "",
        "topic_tag": "",
    }
    fields.update(overrides)
    return fields


def _seed_month(conn) -> None:
    """5 matched, sectioned, scored, themed articles + 2 filtered_out
    (unmatched) articles, all in March 2026 -- enough to exercise every
    rollup table's fetched vs. matched split, all 4 score bands, and a
    multi-article theme.
    """
    insert_article(
        conn,
        **_article(
            url="https://x.test/1", url_norm="https://x.test/1", source="Kalbela",
            review_status="approved", sections="Geopolitical", topic_tag="Middle East / oil",
            llm_score=9.0,
        ),
    )
    insert_article(
        conn,
        **_article(
            url="https://x.test/2", url_norm="https://x.test/2", source="Kalbela",
            review_status="pending", sections="Geopolitical, Macroeconomy", topic_tag="Middle East / oil",
            llm_score=6.5,
        ),
    )
    insert_article(
        conn,
        **_article(
            url="https://x.test/3", url_norm="https://x.test/3", source="Prothom Alo",
            review_status="approved", sections="Macroeconomy", topic_tag="Inflation",
            llm_score=5.2,
        ),
    )
    insert_article(
        conn,
        **_article(
            url="https://x.test/4", url_norm="https://x.test/4", source="Prothom Alo",
            review_status="rejected", sections="Political", topic_tag="Inflation",
            llm_score=2.0,
        ),
    )
    insert_article(
        conn,
        **_article(
            url="https://x.test/5", url_norm="https://x.test/5", source="Kalbela",
            review_status="approved", sections="Geopolitical", topic_tag="", llm_score=None,
        ),
    )
    insert_article(
        conn,
        **_article(url="https://x.test/6", url_norm="https://x.test/6", source="Kalbela", review_status="filtered_out"),
    )
    insert_article(
        conn,
        **_article(
            url="https://x.test/7", url_norm="https://x.test/7", source="Other Source",
            review_status="filtered_out",
        ),
    )
    conn.commit()


def test_compute_rollups_for_day_writes_expected_section_source_band_theme_rows() -> None:
    with migrated_schema() as conn:
        _seed_month(conn)

        counts = compute_rollups_for_day(conn, DAY)
        conn.commit()

        # "watch" is one row per app.db.watchlist entity (6 operators +
        # 7 regulatory axes + 2 wallets) and is written whether or not
        # the month mentioned any of them, so the series has no holes.
        # "econ" is the same guarantee for app.db.econaxes' six economy
        # axes (migrations/0025). "coverage" is the single per-month row
        # of migrations/0021.
        assert counts == {
            "sections": 3,
            "sources": 3,
            "score_bands": 4,
            "themes": 2,
            "watch": len(all_entities()),
            "econ": len(ECON_AXES),
            "coverage": 1,
        }
        assert len(all_entities()) == 15
        assert len(ECON_AXES) == 6

        sections = {
            r["section"]: (r["fetched_count"], r["matched_count"])
            for r in conn.execute(
                "SELECT section, fetched_count, matched_count FROM rollup_monthly_section WHERE month = %s", (MONTH,)
            ).fetchall()
        }
        # Geopolitical appears on articles 1, 2, 5 (all matched).
        assert sections["Geopolitical"] == (3, 3)
        # Macroeconomy appears on articles 2 (matched) and 3 (matched).
        assert sections["Macroeconomy"] == (2, 2)
        # Political appears only on article 4, which is 'rejected' -- still matched (not filtered_out).
        assert sections["Political"] == (1, 1)

        sources = {
            r["source"]: (r["fetched_count"], r["matched_count"])
            for r in conn.execute(
                "SELECT source, fetched_count, matched_count FROM rollup_monthly_source WHERE month = %s", (MONTH,)
            ).fetchall()
        }
        assert sources["Kalbela"] == (4, 3)  # 1,2,5 matched + 6 filtered_out
        assert sources["Prothom Alo"] == (2, 2)
        assert sources["Other Source"] == (1, 0)

        bands = {
            r["band"]: r["article_count"]
            for r in conn.execute(
                "SELECT band, article_count FROM rollup_monthly_score_band WHERE month = %s", (MONTH,)
            ).fetchall()
        }
        assert bands == {"high": 1, "medium": 1, "low": 1, "least": 1}

        themes = {
            r["theme"]: (r["article_count"], float(r["intensity"]))
            for r in conn.execute(
                "SELECT theme, article_count, intensity FROM rollup_monthly_theme WHERE month = %s", (MONTH,)
            ).fetchall()
        }
        assert themes["Middle East / oil"] == (2, 1.0)  # this month's own all-time max so far
        assert themes["Inflation"] == (2, 1.0)


def test_compute_rollups_for_day_is_idempotent_run_twice_same_result() -> None:
    """The mandatory property: running the same day's rollup computation
    twice must not double-count anything -- row counts and every value
    must be byte-identical both times.
    """
    with migrated_schema() as conn:
        _seed_month(conn)

        compute_rollups_for_day(conn, DAY)
        conn.commit()
        snapshot_1 = {
            "sections": conn.execute(
                "SELECT month, section, fetched_count, matched_count FROM rollup_monthly_section ORDER BY section"
            ).fetchall(),
            "sources": conn.execute(
                "SELECT month, source, fetched_count, matched_count FROM rollup_monthly_source ORDER BY source"
            ).fetchall(),
            "bands": conn.execute(
                "SELECT month, band, article_count FROM rollup_monthly_score_band ORDER BY band"
            ).fetchall(),
            "themes": conn.execute(
                "SELECT month, theme, article_count, intensity FROM rollup_monthly_theme ORDER BY theme"
            ).fetchall(),
            "econ": conn.execute(
                "SELECT month, axis, article_count, scored_count, high_count, score_sum "
                "FROM rollup_monthly_econ ORDER BY axis"
            ).fetchall(),
        }

        compute_rollups_for_day(conn, DAY)
        conn.commit()
        snapshot_2 = {
            "sections": conn.execute(
                "SELECT month, section, fetched_count, matched_count FROM rollup_monthly_section ORDER BY section"
            ).fetchall(),
            "sources": conn.execute(
                "SELECT month, source, fetched_count, matched_count FROM rollup_monthly_source ORDER BY source"
            ).fetchall(),
            "bands": conn.execute(
                "SELECT month, band, article_count FROM rollup_monthly_score_band ORDER BY band"
            ).fetchall(),
            "themes": conn.execute(
                "SELECT month, theme, article_count, intensity FROM rollup_monthly_theme ORDER BY theme"
            ).fetchall(),
            "econ": conn.execute(
                "SELECT month, axis, article_count, scored_count, high_count, score_sum "
                "FROM rollup_monthly_econ ORDER BY axis"
            ).fetchall(),
        }

        assert snapshot_1 == snapshot_2
        for key in snapshot_1:
            assert len(snapshot_1[key]) == len(snapshot_2[key])


def test_compute_rollups_for_day_is_idempotent_at_the_module_level_three_runs() -> None:
    """Belt-and-braces: 3 runs, not just 2, to rule out a two-run
    coincidence -- and asserts total row counts across all four tables
    never grow past the first run.
    """
    with migrated_schema() as conn:
        _seed_month(conn)

        def _total_rows() -> int:
            n = 0
            for table in (
                "rollup_monthly_section", "rollup_monthly_source", "rollup_monthly_score_band",
                "rollup_monthly_theme", "rollup_monthly_econ",
            ):
                n += conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608
            return n

        compute_rollups_for_day(conn, DAY)
        conn.commit()
        first_total = _total_rows()

        compute_rollups_for_day(conn, DAY)
        compute_rollups_for_day(conn, DAY)
        conn.commit()
        third_total = _total_rows()

        assert first_total == third_total


def test_a_stale_row_zeroes_out_when_no_longer_present_in_the_recompute() -> None:
    """If a source's only article in a month is deleted (e.g. a purge)
    before the next recompute, that source's row must drop to 0, not be
    left stranded at its previous nonzero value -- the "zero-out missing"
    correction the module docstring describes.
    """
    with migrated_schema() as conn:
        row_id = insert_article(
            conn, **_article(url="https://x.test/only", url_norm="https://x.test/only", source="Ephemeral Source")
        )
        conn.commit()

        compute_rollups_for_day(conn, DAY)
        conn.commit()
        before = conn.execute(
            "SELECT fetched_count FROM rollup_monthly_source WHERE month = %s AND source = %s",
            (MONTH, "Ephemeral Source"),
        ).fetchone()
        assert before["fetched_count"] == 1

        conn.execute("DELETE FROM articles WHERE id = %s", (row_id,))
        conn.commit()

        compute_rollups_for_day(conn, DAY)
        conn.commit()
        after = conn.execute(
            "SELECT fetched_count FROM rollup_monthly_source WHERE month = %s AND source = %s",
            (MONTH, "Ephemeral Source"),
        ).fetchone()
        assert after["fetched_count"] == 0


def test_backfill_all_days_covers_every_distinct_historical_day() -> None:
    with migrated_schema() as conn:
        insert_article(
            conn,
            **_article(
                url="https://x.test/jan", url_norm="https://x.test/jan", day=dt.date(2026, 1, 10),
                sections="Political", review_status="approved",
            ),
        )
        insert_article(
            conn,
            **_article(
                url="https://x.test/feb-a", url_norm="https://x.test/feb-a", day=dt.date(2026, 2, 3),
                sections="Macroeconomy", review_status="approved",
            ),
        )
        insert_article(
            conn,
            **_article(
                url="https://x.test/feb-b", url_norm="https://x.test/feb-b", day=dt.date(2026, 2, 20),
                sections="Macroeconomy", review_status="approved",
            ),
        )
        conn.commit()

        result = backfill_all_days(conn)

        # Two publication months (Jan, Feb) from three articles: the
        # backfill deduplicates to months rather than looping per day,
        # since every call recomputes its whole containing month anyway.
        assert result["publication_months"] == 2

        # THREE months recomputed, not two, and that is the point of
        # migrations/0021's coverage row rather than an off-by-one.
        # `insert_article` leaves `fetched_at` at its `now()` default, so
        # these three articles were COLLECTED in the current real month
        # while being PUBLISHED in January and February. A publication-
        # date-only loop would never visit the collection month, and the
        # dashboard would then show the month monitoring actually
        # happened in as unmonitored.
        assert result["months_recomputed"] == 3
        collection_month = dt.date.today().replace(day=1)
        row = conn.execute(
            "SELECT article_count, collected_days FROM rollup_monthly_coverage WHERE month = %s",
            (collection_month,),
        ).fetchone()
        assert row is not None, "the month the collector ran in must get a coverage row"
        assert row["collected_days"] == 1  # one distinct fetch date
        assert row["article_count"] == 0  # and nothing was published in it

        jan = conn.execute(
            "SELECT matched_count FROM rollup_monthly_section WHERE month = %s AND section = %s",
            (dt.date(2026, 1, 1), "Political"),
        ).fetchone()
        assert jan["matched_count"] == 1

        feb = conn.execute(
            "SELECT matched_count FROM rollup_monthly_section WHERE month = %s AND section = %s",
            (dt.date(2026, 2, 1), "Macroeconomy"),
        ).fetchone()
        assert feb["matched_count"] == 2


def test_backfill_all_days_is_idempotent_run_twice() -> None:
    with migrated_schema() as conn:
        insert_article(
            conn,
            **_article(
                url="https://x.test/a", url_norm="https://x.test/a", day=dt.date(2026, 5, 1),
                sections="Political", review_status="approved",
            ),
        )
        conn.commit()

        backfill_all_days(conn)
        row_count_1 = conn.execute("SELECT count(*) AS n FROM rollup_monthly_section").fetchone()["n"]
        value_1 = conn.execute(
            "SELECT matched_count FROM rollup_monthly_section WHERE month = %s AND section = %s",
            (dt.date(2026, 5, 1), "Political"),
        ).fetchone()["matched_count"]

        backfill_all_days(conn)
        row_count_2 = conn.execute("SELECT count(*) AS n FROM rollup_monthly_section").fetchone()["n"]
        value_2 = conn.execute(
            "SELECT matched_count FROM rollup_monthly_section WHERE month = %s AND section = %s",
            (dt.date(2026, 5, 1), "Political"),
        ).fetchone()["matched_count"]

        assert row_count_1 == row_count_2
        assert value_1 == value_2 == 1


# ── economy axes (migrations/0025) ──────────────────────────────────


def _econ_rows(conn, month: dt.date = MONTH) -> dict[str, dict[str, object]]:
    return {
        r["axis"]: r
        for r in conn.execute(
            "SELECT axis, article_count, scored_count, high_count, score_sum "
            "FROM rollup_monthly_econ WHERE month = %s",
            (month,),
        ).fetchall()
    }


def test_econ_writes_a_row_for_every_axis_even_when_the_month_matched_none() -> None:
    """The no-holes guarantee. A month with no economy coverage must
    still get six rows at zero, or the long-term chart cannot tell "no
    coverage" from "no row" -- the same reason migrations/0021's
    coverage row exists.
    """
    with migrated_schema() as conn:
        insert_article(conn, **_article(url="https://x.test/plain", url_norm="https://x.test/plain"))
        conn.commit()

        compute_rollups_for_day(conn, DAY)
        conn.commit()

        rows = _econ_rows(conn)
        assert set(rows) == {a.key for a in ECON_AXES}
        assert len(rows) == 6
        for axis_key, row in rows.items():
            assert row["article_count"] == 0, axis_key
            assert row["scored_count"] == 0, axis_key
            assert row["high_count"] == 0, axis_key
            assert float(row["score_sum"]) == 0.0, axis_key


def test_econ_counts_volume_and_severity_per_axis() -> None:
    with migrated_schema() as conn:
        # energy: two articles, one of them critical.
        insert_article(conn, **_article(
            url="https://x.test/e1", url_norm="https://x.test/e1", title="",
            title_en="LNG terminal glitch cuts gas supply nationwide", llm_score=9.0,
        ))
        insert_article(conn, **_article(
            url="https://x.test/e2", url_norm="https://x.test/e2", title="",
            title_en="Dhaka joins load-shedding to calm rural anger", llm_score=7.0,
        ))
        # energy, identified but never scored -- counts in article_count only.
        insert_article(conn, **_article(
            url="https://x.test/e3", url_norm="https://x.test/e3", title="তীব্র লোডশেডিংয়ে জনদুর্ভোগ",
            llm_score=None,
        ))
        # capital markets: one, not critical.
        insert_article(conn, **_article(
            url="https://x.test/c1", url_norm="https://x.test/c1", title="",
            title_en="DSEX slips below 5,900 as investors book profits", llm_score=4.0,
        ))
        conn.commit()

        compute_rollups_for_day(conn, DAY)
        conn.commit()

        rows = _econ_rows(conn)
        assert rows["energy"]["article_count"] == 3
        assert rows["energy"]["scored_count"] == 2
        assert rows["energy"]["high_count"] == 1
        assert float(rows["energy"]["score_sum"]) == pytest.approx(16.0)

        assert rows["capital_markets"]["article_count"] == 1
        assert rows["capital_markets"]["scored_count"] == 1
        assert rows["capital_markets"]["high_count"] == 0

        # Untouched axes stay at zero rather than going missing.
        assert rows["banking"]["article_count"] == 0
        assert rows["trade_rmg"]["article_count"] == 0


def test_econ_does_not_import_known_bad_matches() -> None:
    """The precision guards of app/db/econaxes.py, asserted at the
    ROLLUP level -- because that is where a false positive becomes a
    number on a Chief Risk Officer's screen with nobody left to discard
    it. Each headline below is one that really was miscounted before
    its guard landed; see tests/test_econaxes.py for the per-axis
    evidence.
    """
    with migrated_schema() as conn:
        for i, (title, title_en) in enumerate((
            # বিদ্যুৎ ⊂ বিদ্যুৎস্পৃষ্ট -- an electrocution, not energy policy.
            ("বোয়ালখালীতে বিদ্যুৎস্পৃষ্ট হয়ে যুবকের মৃত্যু", ""),
            # পোশাক in its ordinary "clothing" sense.
            ("বিয়ের পোশাকের ফ্যাশন শো, ছিলেন বলিউডের তারকারাও", ""),
            # A nature reserve is not a currency reserve.
            ("", "How Benfield Hill Nature Reserve helps glow worm population"),
            # A dollar figure is not an FX story.
            ("", "The Odyssey earns $258 million in its first week"),
            # ব্যাংকক, not ব্যাংক.
            ("ব্যাংককের উদ্দেশে ঢাকা ছাড়লেন আইজিপি", ""),
            # ভ্যাট ⊂ ভ্যাটিকান.
            ("ভ্যাটিকান ঘোষণাপত্রে সই করলেন অধ্যাপক ইউনূস", ""),
            # Another country's budget.
            ("", "US House Approves $95 Billion Budget Plan With Iran War Funding"),
            # The Indian market live-blog that inflated capital_markets.
            ("", "Stock Market LIVE Updates, Sensex Today: Sensex Drops 650 Points Amid US-Iran Tensions"),
        )):
            insert_article(conn, **_article(
                url=f"https://x.test/bad{i}", url_norm=f"https://x.test/bad{i}",
                title=title, title_en=title_en, llm_score=9.0,
            ))
        conn.commit()

        compute_rollups_for_day(conn, DAY)
        conn.commit()

        rows = _econ_rows(conn)
        for axis_key, row in rows.items():
            assert row["article_count"] == 0, f"{axis_key} claimed a known bad match"
            assert row["high_count"] == 0, f"{axis_key} counted a known bad match as critical"


def test_econ_files_an_electricity_tariff_under_energy_and_not_trade() -> None:
    """The trap the brief calls out, at the rollup level. A bare
    `\\ytariffs?\\y` in trade_rmg counted electricity-tariff stories as
    apparel-export exposure. The headline is not noise -- it is a real
    energy story -- so the assertion is that it lands on exactly the
    right line, which is stronger than asserting it lands nowhere.
    """
    with migrated_schema() as conn:
        insert_article(conn, **_article(
            url="https://x.test/tariff", url_norm="https://x.test/tariff", title="",
            title_en="Electricity Tariff Hike: consumers stare at higher power bills", llm_score=8.0,
        ))
        conn.commit()

        compute_rollups_for_day(conn, DAY)
        conn.commit()

        rows = _econ_rows(conn)
        assert rows["energy"]["article_count"] == 1
        assert rows["energy"]["high_count"] == 1
        assert rows["trade_rmg"]["article_count"] == 0
        assert rows["trade_rmg"]["high_count"] == 0


def test_econ_zeroes_out_when_its_articles_are_purged() -> None:
    """An axis that had coverage and then lost every article to the
    retention purge must drop to 0, not be stranded at its old value.
    Econ rows are rewritten unconditionally every run, so this is the
    same guarantee the score bands get -- asserted because the rollup
    is what outlives the raw rows.
    """
    with migrated_schema() as conn:
        row_id = insert_article(conn, **_article(
            url="https://x.test/e", url_norm="https://x.test/e", title="",
            title_en="LNG terminal glitch cuts gas supply nationwide", llm_score=9.0,
        ))
        conn.commit()
        compute_rollups_for_day(conn, DAY)
        conn.commit()
        assert _econ_rows(conn)["energy"]["article_count"] == 1
        assert _econ_rows(conn)["energy"]["high_count"] == 1

        conn.execute("DELETE FROM articles WHERE id = %s", (row_id,))
        conn.commit()

        compute_rollups_for_day(conn, DAY)
        conn.commit()
        assert _econ_rows(conn)["energy"]["article_count"] == 0
        assert _econ_rows(conn)["energy"]["high_count"] == 0
        assert float(_econ_rows(conn)["energy"]["score_sum"]) == 0.0


def test_econ_rollup_is_idempotent_across_three_runs() -> None:
    with migrated_schema() as conn:
        insert_article(conn, **_article(
            url="https://x.test/e", url_norm="https://x.test/e", title="",
            title_en="Gross forex reserves rise to $37.11b", llm_score=8.0,
        ))
        conn.commit()

        compute_rollups_for_day(conn, DAY)
        conn.commit()
        first = _econ_rows(conn)

        compute_rollups_for_day(conn, DAY)
        compute_rollups_for_day(conn, DAY)
        conn.commit()
        third = _econ_rows(conn)

        assert first == third
        assert first["fx"]["article_count"] == 1
        assert first["fx"]["high_count"] == 1
        assert conn.execute("SELECT count(*) AS n FROM rollup_monthly_econ").fetchone()["n"] == 6

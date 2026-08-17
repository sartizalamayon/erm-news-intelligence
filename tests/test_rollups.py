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

        # "watch" is one row per app.db.watchlist entity (5 operators +
        # 7 regulatory axes) and is written whether or not the month
        # mentioned any of them, so the series has no holes in it.
        # "coverage" is the single per-month row of migrations/0021.
        assert counts == {
            "sections": 3,
            "sources": 3,
            "score_bands": 4,
            "themes": 2,
            "watch": len(all_entities()),
            "coverage": 1,
        }
        assert len(all_entities()) == 12

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
                "rollup_monthly_section", "rollup_monthly_source", "rollup_monthly_score_band", "rollup_monthly_theme"
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

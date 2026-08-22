"""Tests for app.services.evidence -- the shared board statistics and
drill-down layer used by BOTH the telecom watchlist and the economy
axes.

The two things this module must never get wrong are covered hardest:
it must not invent an `llm_reason` it does not have, and it must return
an empty evidence list (not an error, not a fabricated row) for a month
whose raw articles have aged past the retention window.

Postgres-gated (pg_available()), matching every other DB-backed test in
this repo.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.econaxes import axis_by_key, axis_where_sql
from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.db.watchlist import all_entities, entity_predicate_sql
from app.services.evidence import band_for, board_stats, critical_items

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

DAY = dt.date(2026, 3, 15)
SINCE = dt.date(2026, 3, 1)

# Matches nothing and needs no parameters -- lets a test exercise the
# stats/evidence path itself without dragging a real axis pattern in.
ALL_ROWS = ("TRUE", [])


def _add(conn, url: str, **overrides) -> int:
    fields: dict[str, object] = {
        "url": url,
        "url_norm": url,
        "source": "Kalbela",
        "title": "Headline",
        "day": DAY,
        "review_status": "pending",
    }
    fields.update(overrides)
    return insert_article(conn, **fields)


# ── band_for ────────────────────────────────────────────────────────


def test_band_for_matches_the_scoring_rubric() -> None:
    """The rubric in app/services/llm/prompts.py: RED 8-10, YELLOW 6-7,
    GREEN 5, MAGENTA 1-4 -- the same boundaries app/db/rollups.py writes
    as high/medium/low/least, so a band means one thing everywhere.
    """
    assert [band_for(s) for s in (10.0, 9.0, 8.0)] == ["red"] * 3
    assert [band_for(s) for s in (7.0, 6.5, 6.0)] == ["yellow"] * 3
    assert [band_for(s) for s in (5.9, 5.0)] == ["green"] * 2
    assert [band_for(s) for s in (4.9, 4.0, 1.0)] == ["magenta"] * 3


def test_band_boundaries_are_inclusive_at_the_bottom() -> None:
    assert band_for(8.0) == "red" and band_for(7.999) == "yellow"
    assert band_for(6.0) == "yellow" and band_for(5.999) == "green"
    assert band_for(5.0) == "green" and band_for(4.999) == "magenta"


# ── board_stats ─────────────────────────────────────────────────────


def test_board_stats_counts_items_scored_critical_and_mean() -> None:
    with migrated_schema() as conn:
        _add(conn, "u1", llm_score=9.0)
        _add(conn, "u2", llm_score=8.0)
        _add(conn, "u3", llm_score=6.0)
        _add(conn, "u4", llm_score=None)  # identified but never scored
        conn.commit()

        stats = board_stats(conn, *ALL_ROWS, since=SINCE)
        assert stats.items == 4
        assert stats.scored == 3
        assert stats.critical == 2  # 9.0 and 8.0
        assert stats.mean == pytest.approx((9.0 + 8.0 + 6.0) / 3)


def test_board_stats_mean_is_none_not_zero_when_nothing_was_scored() -> None:
    """"No scored articles" and "a mean of zero" are different states.
    A caller must be able to tell them apart before drawing a trend.
    """
    with migrated_schema() as conn:
        _add(conn, "u1", llm_score=None)
        _add(conn, "u2", llm_score=None)
        conn.commit()

        stats = board_stats(conn, *ALL_ROWS, since=SINCE)
        assert stats.items == 2
        assert stats.scored == 0
        assert stats.critical == 0
        assert stats.mean is None


def test_board_stats_is_empty_and_not_an_error_for_a_month_with_no_rows() -> None:
    with migrated_schema() as conn:
        stats = board_stats(conn, *ALL_ROWS, since=SINCE)
        assert stats == type(stats)(items=0, scored=0, critical=0, mean=None)


def test_board_stats_honours_the_since_boundary() -> None:
    with migrated_schema() as conn:
        _add(conn, "old", day=dt.date(2026, 2, 27), llm_score=9.0)
        _add(conn, "new", day=DAY, llm_score=9.0)
        conn.commit()

        assert board_stats(conn, *ALL_ROWS, since=SINCE).items == 1
        assert board_stats(conn, *ALL_ROWS, since=dt.date(2026, 1, 1)).items == 2


# ── critical_items ──────────────────────────────────────────────────


def test_critical_items_orders_by_score_then_day_descending() -> None:
    with migrated_schema() as conn:
        _add(conn, "a", llm_score=8.0, day=dt.date(2026, 3, 2), title="older eight")
        _add(conn, "b", llm_score=10.0, day=dt.date(2026, 3, 1), title="the ten")
        _add(conn, "c", llm_score=8.0, day=dt.date(2026, 3, 20), title="newer eight")
        conn.commit()

        items = critical_items(conn, *ALL_ROWS, since=SINCE, limit=10)
        assert [i.title for i in items] == ["the ten", "newer eight", "older eight"]


def test_critical_items_respects_limit_and_min_score() -> None:
    with migrated_schema() as conn:
        for i, score in enumerate((9.0, 8.5, 8.0, 7.0, 3.0)):
            _add(conn, f"u{i}", llm_score=score)
        conn.commit()

        assert len(critical_items(conn, *ALL_ROWS, since=SINCE)) == 3  # default limit
        assert len(critical_items(conn, *ALL_ROWS, since=SINCE, limit=10)) == 3  # only 3 are >= 8
        assert len(critical_items(conn, *ALL_ROWS, since=SINCE, limit=10, min_score=6.0)) == 4
        assert len(critical_items(conn, *ALL_ROWS, since=SINCE, limit=2)) == 2


def test_critical_items_prefers_title_en_but_falls_back_to_title() -> None:
    """The `title_en or title` convention the Python side already uses
    (brief.py, export.py, near_duplicates.py), expressed in SQL. An
    empty `title_en` is the untranslated state, not a real title.
    """
    with migrated_schema() as conn:
        _add(conn, "a", llm_score=9.0, title="বাংলা শিরোনাম", title_en="English headline")
        _add(conn, "b", llm_score=8.5, title="শুধু বাংলা", title_en="")
        conn.commit()

        items = critical_items(conn, *ALL_ROWS, since=SINCE, limit=10)
        assert items[0].title == "English headline"
        assert items[1].title == "শুধু বাংলা"


def test_critical_items_never_fabricates_a_reason() -> None:
    """The load-bearing one. A reader told WHY an article is critical
    will act on it, so an invented reason is worse than a blank. When
    `llm_reason` is empty the item carries an empty string and the
    caller renders an honest absence.
    """
    with migrated_schema() as conn:
        _add(conn, "a", llm_score=9.0, title="has a reason", llm_reason="Direct GP revenue exposure.")
        _add(conn, "b", llm_score=8.5, title="no reason", llm_reason="")
        _add(conn, "c", llm_score=8.2, title="blank reason", llm_reason="   ")
        conn.commit()

        by_title = {i.title: i for i in critical_items(conn, *ALL_ROWS, since=SINCE, limit=10)}
        assert by_title["has a reason"].reason == "Direct GP revenue exposure."
        assert by_title["no reason"].reason == ""
        assert by_title["blank reason"].reason == ""
        # And nothing was substituted in from the headline or elsewhere.
        assert by_title["no reason"].reason != by_title["no reason"].title


def test_critical_items_carries_the_band_and_the_source_row() -> None:
    with migrated_schema() as conn:
        row_id = _add(conn, "a", llm_score=9.0, source="Prothom Alo", title="t", llm_reason="r")
        conn.commit()

        item = critical_items(conn, *ALL_ROWS, since=SINCE)[0]
        assert item.article_id == row_id
        assert item.score == pytest.approx(9.0)
        assert item.band == "red"
        assert item.source == "Prothom Alo"
        assert item.day == DAY


def test_critical_items_returns_empty_when_articles_have_aged_out() -> None:
    """RETENTION, the case the UI must render honestly.

    `app/services/storage.py` purges raw `articles` on the
    `raw_retention_days` window while the monthly rollups survive. So an
    old month legitimately has a real `high_count` in the rollup and NO
    live rows behind it. That must come back as `[]` -- a real state to
    show as "the headlines have aged out" -- not an exception and not a
    fabricated placeholder row.
    """
    with migrated_schema() as conn:
        row_id = _add(conn, "a", llm_score=9.0)
        conn.commit()
        assert len(critical_items(conn, *ALL_ROWS, since=SINCE)) == 1
        before = board_stats(conn, *ALL_ROWS, since=SINCE)
        assert before.critical == 1

        # The purge.
        conn.execute("DELETE FROM articles WHERE id = %s", (row_id,))
        conn.commit()

        assert critical_items(conn, *ALL_ROWS, since=SINCE) == []
        assert board_stats(conn, *ALL_ROWS, since=SINCE).critical == 0


# ── shared by both boards ───────────────────────────────────────────


def test_the_same_functions_serve_an_econ_axis_and_a_watchlist_entity() -> None:
    """The reason this module exists: one implementation, two boards.
    An econ axis supplies (sql, params); a watchlist entity supplies
    (sql, []). Neither is privileged.
    """
    with migrated_schema() as conn:
        _add(conn, "energy", llm_score=9.0, title="", title_en="LNG supply cut hits national grid",
             llm_reason="Tower opex exposure.")
        _add(conn, "telecom", llm_score=9.0, title="", title_en="BTRC fines Grameenphone over SIM rules",
             llm_reason="Direct regulatory action.")
        conn.commit()

        axis = axis_by_key("energy")
        assert axis is not None
        axis_sql, axis_params = axis_where_sql(axis)
        energy = critical_items(conn, axis_sql, axis_params, since=SINCE, limit=10)
        assert [i.title for i in energy] == ["LNG supply cut hits national grid"]
        assert board_stats(conn, axis_sql, axis_params, since=SINCE).items == 1

        entity_sql = entity_predicate_sql(next(e for _g, e in all_entities() if e.key == "btrc"))
        btrc = critical_items(conn, entity_sql, [], since=SINCE, limit=10)
        assert [i.title for i in btrc] == ["BTRC fines Grameenphone over SIM rules"]
        assert board_stats(conn, entity_sql, [], since=SINCE).items == 1

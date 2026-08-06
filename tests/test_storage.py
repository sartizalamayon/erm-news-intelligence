"""Storage accounting and purge (DoD #4, #5): real Postgres
introspection, the ceiling as configuration, dry-run vs. confirmed
purges, and the export-first gate.

See also the RT-6 regression tests near the bottom of this file
(`test_confirmed_purge_never_exceeds_the_previewed_snapshot_*`,
`test_confirm_without_a_prior_preview_is_refused`,
`test_preview_snapshot_cannot_be_replayed_twice`) — these prove the fix
for docs/red-team-findings.md RT-6 ("purge dry-run preview count can
silently diverge from the real delete count"). The corresponding
xfail(strict) proving test lives in
`tests/test_redteam_purge_dry_run_toctou.py`, owned by RED-TEAM — do
not edit that file; it is expected to XPASS (and therefore report as a
failure under strict xfail) once this fix is in place, which is RED-TEAM's
signal to remove the marker, not a regression here.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.queries import insert_article
from app.db.settings import set_setting
from app.db.testing import migrated_schema, pg_available
from app.errors import ValidationError
from app.services.storage import (
    get_storage_usage,
    purge_matched_articles,
    purge_stale_bodies,
    purge_stale_unmatched_articles,
)

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def _article(**overrides: object) -> dict[str, object]:
    fields = {
        "url": "https://example.com/x",
        "url_norm": "https://example.com/x",
        "source": "Example",
        "title": "Headline",
        "day": dt.date.today(),
        "review_status": "pending",
    }
    fields.update(overrides)
    return fields


def _stale_unmatched(slug: str, day: dt.date) -> dict[str, object]:
    """A filtered_out (unmatched) article old enough to qualify for
    purge_stale_unmatched_articles -- the shape the RT-6 regression tests
    below repeat with a different `slug` per row.
    """
    url = f"https://x.test/{slug}"
    return _article(url=url, url_norm=url, day=day, review_status="filtered_out")


def test_storage_usage_reflects_real_introspection_and_configured_ceiling() -> None:
    with migrated_schema() as conn:
        insert_article(conn, **_article(url="https://example.com/1", url_norm="https://example.com/1"))
        conn.commit()

        usage = get_storage_usage(conn)
        assert usage.database_size_bytes > 0
        assert usage.ceiling_bytes == 500 * 1024 * 1024  # the seeded default
        assert "articles" in usage.table_sizes_bytes
        assert usage.table_sizes_bytes["articles"] > 0
        assert 0 <= usage.pct_used < 100

        set_setting(conn, "storage_ceiling_mb", "1")
        conn.commit()
        usage_tiny_ceiling = get_storage_usage(conn)
        assert usage_tiny_ceiling.ceiling_bytes == 1 * 1024 * 1024
        assert usage_tiny_ceiling.pct_used > usage.pct_used


def test_purge_stale_bodies_dry_run_does_not_delete() -> None:
    with migrated_schema() as conn:
        article_id = insert_article(conn, **_article())
        conn.execute(
            "INSERT INTO article_bodies (article_id, content_text, fetched_at) "
            "VALUES (%s, %s, now() - interval '400 days')",
            (article_id, "some body text"),
        )
        conn.commit()

        preview = purge_stale_bodies(conn, older_than_days=60, confirm=False)
        assert preview.confirmed is False
        assert preview.rows_affected == 1

        remaining = conn.execute("SELECT count(*) AS n FROM article_bodies").fetchone()["n"]
        assert remaining == 1  # dry run must not mutate


def test_purge_stale_bodies_confirmed_deletes_and_marks_purged() -> None:
    with migrated_schema() as conn:
        article_id = insert_article(conn, **_article())
        conn.execute(
            "INSERT INTO article_bodies (article_id, content_text, fetched_at) "
            "VALUES (%s, %s, now() - interval '400 days')",
            (article_id, "old body"),
        )
        conn.commit()

        purge_stale_bodies(conn, older_than_days=60, confirm=False)  # required: seeds the snapshot confirm=True reads
        result = purge_stale_bodies(conn, older_than_days=60, confirm=True)
        conn.commit()
        assert result.confirmed is True
        assert result.rows_affected == 1

        remaining = conn.execute("SELECT count(*) AS n FROM article_bodies").fetchone()["n"]
        assert remaining == 0
        status = conn.execute("SELECT body_status FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert status["body_status"] == "purged"


def test_purge_stale_unmatched_articles_preserves_dedup_via_seen_urls() -> None:
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=120)
        article_id = insert_article(
            conn,
            **_article(
                url="https://example.com/old-unmatched",
                url_norm="https://example.com/old-unmatched",
                day=old_day,
                review_status="filtered_out",
            ),
        )
        assert article_id is not None
        conn.commit()

        purge_stale_unmatched_articles(conn, older_than_days=90, confirm=False)  # seeds the snapshot
        result = purge_stale_unmatched_articles(conn, older_than_days=90, confirm=True)
        conn.commit()
        assert result.rows_affected == 1

        gone = conn.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert gone is None
        remembered = conn.execute(
            "SELECT 1 FROM seen_urls WHERE url_norm = %s", ("https://example.com/old-unmatched",)
        ).fetchone()
        assert remembered is not None


def test_purge_stale_unmatched_articles_never_touches_pending_or_approved() -> None:
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=120)
        pending_id = insert_article(
            conn,
            **_article(
                url="https://x.test/still-pending",
                url_norm="https://x.test/still-pending",
                day=old_day,
                review_status="pending",
            ),
        )
        conn.commit()

        purge_stale_unmatched_articles(conn, older_than_days=90, confirm=False)  # seeds the (empty) snapshot
        purge_stale_unmatched_articles(conn, older_than_days=90, confirm=True)
        conn.commit()

        still_there = conn.execute("SELECT 1 FROM articles WHERE id = %s", (pending_id,)).fetchone()
        assert still_there is not None


def test_purge_matched_articles_require_exported_blocks_unexported_rows() -> None:
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=400)
        insert_article(
            conn, **_article(url="https://x.test/never-exported", url_norm="https://x.test/never-exported", day=old_day)
        )
        conn.commit()

        preview = purge_matched_articles(conn, older_than=dt.date.today(), require_exported=True, confirm=False)
        assert preview.rows_affected == 0  # nothing eligible -- never exported

        result = purge_matched_articles(conn, older_than=dt.date.today(), require_exported=True, confirm=True)
        conn.commit()
        assert result.rows_affected == 0
        still_there = conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]
        assert still_there == 1


def test_purge_matched_articles_allows_exported_rows() -> None:
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=400)
        article_id = insert_article(
            conn, **_article(url="https://x.test/exported", url_norm="https://x.test/exported", day=old_day)
        )
        conn.execute("UPDATE articles SET exported_at = now() WHERE id = %s", (article_id,))
        conn.commit()

        purge_matched_articles(conn, older_than=dt.date.today(), require_exported=True, confirm=False)  # seeds snapshot
        result = purge_matched_articles(conn, older_than=dt.date.today(), require_exported=True, confirm=True)
        conn.commit()
        assert result.rows_affected == 1
        assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 0


# ── RT-6 regression tests (docs/red-team-findings.md) ───────────────────
#
# RT-6: the confirm=False preview and confirm=True delete used to be two
# separate, unlocked queries re-running the identical time-relative WHERE
# clause -- a row that became eligible in the gap between them (e.g. the
# daily batch job inserting more stale rows while an admin was looking at
# the preview) was silently deleted without ever appearing in what the
# operator approved. Fixed by having confirm=False snapshot the exact
# qualifying ids and confirm=True require and delete exactly that
# snapshot (app/services/storage.py's module docstring, "Closing RT-6").


def test_confirmed_purge_never_exceeds_the_previewed_snapshot_unmatched() -> None:
    """The exact scenario RT-6/tests/test_redteam_purge_dry_run_toctou.py
    reproduces, proven fixed here (that file stays untouched and is
    RED-TEAM's to flip): preview shows N rows; a second, independently
    qualifying row is inserted in the gap; confirm must still delete only
    the N rows that were actually previewed, and the late-arriving row
    must survive.
    """
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=400)
        first_id = insert_article(conn, **_stale_unmatched("toctou-1", old_day))
        conn.commit()

        preview = purge_stale_unmatched_articles(conn, older_than_days=30, confirm=False)
        assert preview.rows_affected == 1
        assert preview.row_ids == [first_id]
        shown_to_operator = preview.rows_affected

        # The gap: something else (the daily batch job, another admin
        # action, anything) inserts a second row that independently
        # qualifies under the identical WHERE clause the preview already ran.
        second_id = insert_article(conn, **_stale_unmatched("toctou-2", old_day))
        conn.commit()

        confirmed = purge_stale_unmatched_articles(conn, older_than_days=30, confirm=True)
        conn.commit()

        assert confirmed.rows_affected == shown_to_operator == 1
        assert confirmed.row_ids == [first_id]

        assert conn.execute("SELECT 1 FROM articles WHERE id = %s", (first_id,)).fetchone() is None
        # The late-arriving row was never shown to the operator -- it must survive this confirm.
        assert conn.execute("SELECT 1 FROM articles WHERE id = %s", (second_id,)).fetchone() is not None


def test_confirmed_purge_never_exceeds_the_previewed_snapshot_bodies() -> None:
    """Same race, exercised against purge_stale_bodies (the other
    automatic/routine purge) rather than purge_stale_unmatched_articles.
    """
    with migrated_schema() as conn:
        first_article = insert_article(conn, **_article(url="https://x.test/body-1", url_norm="https://x.test/body-1"))
        conn.execute(
            "INSERT INTO article_bodies (article_id, content_text, fetched_at) "
            "VALUES (%s, %s, now() - interval '400 days')",
            (first_article, "old body one"),
        )
        conn.commit()

        preview = purge_stale_bodies(conn, older_than_days=60, confirm=False)
        assert preview.rows_affected == 1
        shown_to_operator = preview.rows_affected

        second_article = insert_article(conn, **_article(url="https://x.test/body-2", url_norm="https://x.test/body-2"))
        conn.execute(
            "INSERT INTO article_bodies (article_id, content_text, fetched_at) "
            "VALUES (%s, %s, now() - interval '400 days')",
            (second_article, "old body two"),
        )
        conn.commit()

        confirmed = purge_stale_bodies(conn, older_than_days=60, confirm=True)
        conn.commit()

        assert confirmed.rows_affected == shown_to_operator == 1
        remaining = conn.execute("SELECT article_id FROM article_bodies").fetchall()
        assert [row["article_id"] for row in remaining] == [second_article]


def test_confirm_without_a_prior_preview_is_refused() -> None:
    """There is no code path left that lets confirm=True fall back to a
    freshly-recomputed WHERE clause -- without a matching preview on
    record it must refuse outright, not "helpfully" delete based on live
    data (which is the exact behavior RT-6 flagged).
    """
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=400)
        insert_article(conn, **_stale_unmatched("no-preview", old_day))
        conn.commit()

        with pytest.raises(ValidationError, match="no matching dry-run preview"):
            purge_stale_unmatched_articles(conn, older_than_days=30, confirm=True)

        # nothing was deleted by the refused call
        still_there = conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]
        assert still_there == 1


def test_preview_snapshot_cannot_be_replayed_twice() -> None:
    """A snapshot is consumed on first use -- a second confirm=True call
    reusing the same preview must be refused, not silently re-delete (now
    zero rows) or double-count.
    """
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=400)
        insert_article(conn, **_stale_unmatched("replay", old_day))
        conn.commit()

        purge_stale_unmatched_articles(conn, older_than_days=30, confirm=False)
        first_confirm = purge_stale_unmatched_articles(conn, older_than_days=30, confirm=True)
        conn.commit()
        assert first_confirm.rows_affected == 1

        with pytest.raises(ValidationError, match="no matching dry-run preview"):
            purge_stale_unmatched_articles(conn, older_than_days=30, confirm=True)


def test_preview_snapshot_expires_after_staleness_window() -> None:
    """A forgotten preview from long ago shouldn't silently authorize a
    delete now -- simulated by backdating the persisted snapshot's
    timestamp past `_PREVIEW_STALENESS` directly (real time doesn't need
    to pass for this test).
    """
    import json

    from app.services.storage import _PREVIEW_STALENESS, _preview_snapshot_key

    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=400)
        insert_article(conn, **_stale_unmatched("stale-preview", old_day))
        conn.commit()

        purge_stale_unmatched_articles(conn, older_than_days=30, confirm=False)

        # Backdate the snapshot's created_at past the staleness window.
        key = _preview_snapshot_key("purge_stale_unmatched_articles", "older_than_days=30")
        row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
        payload = json.loads(row["value"])
        backdated = dt.datetime.now(dt.timezone.utc) - _PREVIEW_STALENESS - dt.timedelta(minutes=1)
        payload["created_at"] = backdated.isoformat()
        set_setting(conn, key, json.dumps(payload))
        conn.commit()

        with pytest.raises(ValidationError, match="no matching dry-run preview"):
            purge_stale_unmatched_articles(conn, older_than_days=30, confirm=True)

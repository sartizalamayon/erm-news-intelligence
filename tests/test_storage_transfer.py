"""Export / import round-trip (DoD #6): "exporting then re-importing
must reproduce equivalent rows" — proven here by exporting real rows
(including one with a body, one with a NULL published date, and one
with a self-referencing topic_parent_id), wiping the table, importing
the file back, and comparing every column.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.services.data_transfer import export_to_jsonl, import_from_jsonl

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def _article(**overrides: object) -> dict[str, object]:
    fields = {
        "url": "https://example.com/x",
        "url_norm": "https://example.com/x",
        "source": "Example",
        "title": "Headline",
        "day": dt.date(2026, 8, 5),
        "review_status": "pending",
    }
    fields.update(overrides)
    return fields


def _snapshot(conn, table: str, order_by: str) -> list[dict]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
    return [{k: v for k, v in row.items() if k != "search_vec"} for row in rows]


def test_export_then_import_reproduces_equivalent_rows(tmp_path) -> None:
    with migrated_schema() as conn:
        parent_id = insert_article(conn, **_article(url="https://example.com/parent", url_norm="https://example.com/parent"))
        child_id = insert_article(
            conn,
            **_article(
                url="https://example.com/child",
                url_norm="https://example.com/child",
                topic_parent_id=parent_id,
                llm_score=7.5,
                published=dt.datetime(2026, 8, 5, 8, 42, tzinfo=dt.timezone(dt.timedelta(hours=6))),
            ),
        )
        undated_id = insert_article(
            conn, **_article(url="https://example.com/undated", url_norm="https://example.com/undated", published=None)
        )
        conn.execute(
            "INSERT INTO article_bodies (article_id, content_text) VALUES (%s, %s)",
            (child_id, "Full body text with unicode: বাংলা।"),
        )
        conn.commit()

        before_articles = _snapshot(conn, "articles", "id")
        before_bodies = _snapshot(conn, "article_bodies", "article_id")
        assert len(before_articles) == 3
        assert len(before_bodies) == 1

        export_path = tmp_path / "export.jsonl.gz"
        manifest = export_to_jsonl(conn, export_path, mark_exported=True)
        conn.commit()
        assert manifest.articles_exported == 3
        assert manifest.bodies_exported == 1

        # exported_at is a real side effect of export -- excluded from the
        # equivalence comparison below (it changes by definition), everything
        # else must match exactly.
        exported_at_before = {row["id"]: row["exported_at"] for row in before_articles}
        for row in before_articles:
            assert exported_at_before[row["id"]] is None  # confirm it really was unset pre-export

        # Wipe and re-import -- the "delete to reclaim space, re-import
        # later" journey (ADR §9.2).
        conn.execute("DELETE FROM articles")
        conn.commit()
        assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM article_bodies").fetchone()["n"] == 0

        report = import_from_jsonl(conn, export_path)
        conn.commit()
        assert report.articles_imported == 3
        assert report.bodies_imported == 1

        after_articles = _snapshot(conn, "articles", "id")
        after_bodies = _snapshot(conn, "article_bodies", "article_id")

        assert len(after_articles) == 3
        assert len(after_bodies) == 1

        for before_row, after_row in zip(before_articles, after_articles, strict=True):
            for col in before_row:
                if col == "exported_at":
                    continue  # expected to differ: export stamps it, re-import doesn't reset it
                assert before_row[col] == after_row[col], f"column {col} mismatch for id={before_row['id']}"

        assert before_bodies == after_bodies

        # Referential integrity survived the round trip through the
        # two-pass topic_parent_id resolution.
        child_after = next(r for r in after_articles if r["id"] == child_id)
        assert child_after["topic_parent_id"] == parent_id

        undated_after = next(r for r in after_articles if r["id"] == undated_id)
        assert undated_after["published"] is None


def test_import_is_idempotent_on_a_second_run(tmp_path) -> None:
    with migrated_schema() as conn:
        insert_article(conn, **_article(url="https://example.com/only", url_norm="https://example.com/only"))
        conn.commit()

        export_path = tmp_path / "export.jsonl.gz"
        export_to_jsonl(conn, export_path, mark_exported=False)
        conn.commit()

        report_first = import_from_jsonl(conn, export_path)
        conn.commit()
        # row already exists (never deleted in this test) -- both counted as skipped, not duplicated
        assert report_first.articles_imported == 0
        assert report_first.articles_skipped_existing == 1
        assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 1

        report_second = import_from_jsonl(conn, export_path)
        conn.commit()
        assert report_second.articles_imported == 0
        assert report_second.articles_skipped_existing == 1
        assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 1


def test_export_scoped_by_day_range(tmp_path) -> None:
    with migrated_schema() as conn:
        insert_article(
            conn, **_article(url="https://x.test/in-range", url_norm="https://x.test/in-range", day=dt.date(2026, 8, 5))
        )
        insert_article(
            conn,
            **_article(
                url="https://x.test/out-of-range", url_norm="https://x.test/out-of-range", day=dt.date(2026, 1, 1)
            ),
        )
        conn.commit()

        export_path = tmp_path / "scoped.jsonl.gz"
        manifest = export_to_jsonl(conn, export_path, day_from=dt.date(2026, 8, 1), day_to=dt.date(2026, 8, 31))
        conn.commit()

        assert manifest.articles_exported == 1

        exported_flag = conn.execute(
            "SELECT url, exported_at FROM articles ORDER BY url"
        ).fetchall()
        by_url = {row["url"]: row["exported_at"] for row in exported_flag}
        assert by_url["https://x.test/in-range"] is not None
        assert by_url["https://x.test/out-of-range"] is None

"""`sources.registry_key` has to survive the order a deploy really runs in.

THE DEFECT THESE TESTS PIN
──────────────────────────
`registry_key` (migrations/0014) correlates a `sources` row with the
code-side fetch plugin. The enable/disable veto
(`app.services.collector._filter_enabled`), the Sources screen's
per-source test/diagnose (`app.services.admin._plugin_for_registry_key`)
and the dashboard's "sources ready" denominator all resolve through it.

No migration ever INSERTs into `sources`. The rows come from
`migrations/legacy/import_legacy_sqlite.py`, which is deliberately not
part of the migration chain and, by its own documented usage, runs
AFTER it. 0014 and 0022 are pure `UPDATE ... WHERE name = ...`, so on a
fresh deploy they both change 0 rows against an empty table, get
recorded applied and checksummed, and can never run again. Every
imported row then carries `registry_key = NULL` forever: the Sources
toggle silently does nothing, diagnostics resolve no plugin, and the
fetched-today tile read "0 of 0 sources responded" on a day 45 plugins
had run.

The fix is in two halves, and both are exercised below:

  * `migrations/0028_repair_source_registry_keys.sql` repairs rows that
    are ALREADY in the table when the chain runs.
  * `app.services.collector.reconcile_source_registry_keys`, called by
    `run_collection` on every run, repairs rows that arrive at any point
    afterwards. This is the half that is order-independent, and the
    reason this defect cannot come back the next time someone imports a
    source list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.testing import migrated_schema, pg_available
from app.services import collector
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.registry import all_sources, load_all

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

MIGRATION_0028 = Path(__file__).resolve().parents[1] / "migrations" / "0028_repair_source_registry_keys.sql"

# Three real (name, key) pairs, read the way the legacy `sources` table
# holds them. Named explicitly rather than derived so the test says what
# a row actually looks like.
LEGACY_ROWS = [
    ("Prothom Alo", "prothom_alo"),
    ("The Business Standard", "business_standard"),
    ("Amarbarta", "amar_barta"),
]


def _import_legacy_style_rows(conn, rows=LEGACY_ROWS) -> None:
    """Exactly what `import_legacy_sqlite.migrate_sources` writes: name,
    method, config, language, enabled — and no registry_key, because the
    legacy schema has no such column.
    """
    for name, _key in rows:
        conn.execute(
            "INSERT INTO sources (name, method, config, language, enabled) VALUES (%s, %s, %s::jsonb, %s, %s)",
            (name, "rss", "{}", "mixed", True),
        )
    conn.commit()


def _keys_by_name(conn) -> dict[str, str | None]:
    return {r["name"]: r["registry_key"] for r in conn.execute("SELECT name, registry_key FROM sources").fetchall()}


class _QuietSource(NewsSource):
    """A registered key's stand-in that never touches the network."""

    key = "prothom_alo"
    display_name = "Prothom Alo"

    def iter_feed_items(self) -> list[FeedItem]:
        return []

    def fetch_article(self, item: FeedItem) -> FetchedArticle:  # pragma: no cover - never called
        raise NotImplementedError


class _OtherQuietSource(_QuietSource):
    key = "business_standard"
    display_name = "The Business Standard"


# ── the ordering problem itself ─────────────────────────────────────────


def test_rows_imported_after_the_migration_chain_arrive_unkeyed() -> None:
    """The premise, asserted rather than assumed: a fully migrated
    database (0014, 0022 AND 0028 all applied and checksummed) still
    hands out NULL keys to rows imported afterwards, because a migration
    cannot repair what does not exist yet. This is why the repair also
    has to live on the collection path.
    """
    with migrated_schema() as conn:
        _import_legacy_style_rows(conn)
        assert set(_keys_by_name(conn).values()) == {None}


def test_reconcile_keys_rows_that_arrived_after_every_migration_had_run() -> None:
    with migrated_schema() as conn:
        _import_legacy_style_rows(conn)

        filled = collector.reconcile_source_registry_keys(conn)

        assert filled == dict(LEGACY_ROWS)
        assert _keys_by_name(conn) == dict(LEGACY_ROWS)


def test_reconcile_is_idempotent() -> None:
    with migrated_schema() as conn:
        _import_legacy_style_rows(conn)
        collector.reconcile_source_registry_keys(conn)
        assert collector.reconcile_source_registry_keys(conn) == {}
        assert _keys_by_name(conn) == dict(LEGACY_ROWS)


# ── the consequences, one test each ─────────────────────────────────────


def test_the_disable_toggle_is_inert_without_a_key_and_works_with_one() -> None:
    """The Sources screen's switch. `_filter_enabled` correlates on
    `registry_key`, so `enabled = FALSE` on an unkeyed row is a vote
    nothing can count — the source keeps being fetched and the operator
    watches a switch that does nothing.
    """
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO sources (name, method, enabled) VALUES (%s, %s, %s)", ("Prothom Alo", "rss", False)
        )
        conn.commit()
        load_all()
        registry = all_sources()

        before = collector.enabled_source_classes(conn, registry)
        assert "prothom_alo" in before, "unkeyed row: the DB has no way to veto, which is the bug"

        collector.reconcile_source_registry_keys(conn)

        after = collector.enabled_source_classes(conn, registry)
        assert "prothom_alo" not in after
        assert len(after) == len(registry) - 1


def test_run_collection_reconciles_before_it_decides_what_to_fetch() -> None:
    """End to end on the real path: an unkeyed, disabled row must veto
    its plugin in the SAME run that keys it, not one run later.
    """
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO sources (name, method, enabled) VALUES (%s, %s, %s)", ("Prothom Alo", "rss", False)
        )
        conn.execute(
            "INSERT INTO sources (name, method, enabled) VALUES (%s, %s, %s)",
            ("The Business Standard", "rss", True),
        )
        conn.commit()

        result = collector.run_collection(
            conn,
            source_classes={"prothom_alo": _QuietSource, "business_standard": _OtherQuietSource},
            max_workers=1,
        )

        ran = {s.source_key for s in result.sources}
        assert ran == {"business_standard"}
        assert _keys_by_name(conn) == {"Prothom Alo": "prothom_alo", "The Business Standard": "business_standard"}


# ── the guards: this only ever fills a NULL, and never guesses ──────────


def test_reconcile_never_overwrites_a_key_someone_else_set() -> None:
    """A key an admin (or a better correlation) has set outranks a name
    match — the whole reason 0014 introduced the column was that `name`
    is admin-editable.
    """
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO sources (name, method, registry_key, enabled) VALUES (%s, %s, %s, %s)",
            ("Prothom Alo", "rss", "daily_star", True),
        )
        conn.commit()

        assert collector.reconcile_source_registry_keys(conn) == {}
        assert _keys_by_name(conn) == {"Prothom Alo": "daily_star"}


def test_matching_on_name_is_unambiguous_because_the_schema_says_so() -> None:
    """Name matching is only safe because `sources.name` is UNIQUE
    (migrations/0002), so a name can never designate two rows. Both
    halves of the repair still carry an explicit ambiguity guard, but
    this constraint is the reason they can never be exercised — pinned
    here so dropping it shows up as a failing test rather than as a
    source silently keyed to the wrong plugin.
    """
    import psycopg

    with migrated_schema() as conn:
        conn.execute("INSERT INTO sources (name, method, enabled) VALUES (%s, %s, %s)", ("Prothom Alo", "rss", True))
        conn.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "INSERT INTO sources (name, method, enabled) VALUES (%s, %s, %s)", ("Prothom Alo", "rss", True)
            )
        conn.rollback()


def test_reconcile_skips_a_key_another_row_already_holds() -> None:
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO sources (name, method, registry_key, enabled) VALUES (%s, %s, %s, %s)",
            ("Prothom Alo (old row)", "rss", "prothom_alo", True),
        )
        conn.execute("INSERT INTO sources (name, method, enabled) VALUES (%s, %s, %s)", ("Prothom Alo", "rss", True))
        conn.commit()

        assert collector.reconcile_source_registry_keys(conn) == {}
        assert _keys_by_name(conn)["Prothom Alo"] is None


def test_reconcile_leaves_a_name_no_plugin_answers_to_alone() -> None:
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO sources (name, method, enabled) VALUES (%s, %s, %s)", ("A Paper Nobody Ported", "rss", True)
        )
        conn.commit()

        assert collector.reconcile_source_registry_keys(conn) == {}
        assert _keys_by_name(conn) == {"A Paper Nobody Ported": None}


# ── migration 0028: the same repair, for rows already in the table ──────


def test_migration_0028_keys_rows_that_are_already_there() -> None:
    """Run the shipped statement against a table that already holds
    unkeyed rows — the state every environment that has run the legacy
    import is in today.
    """
    with migrated_schema() as conn:
        _import_legacy_style_rows(conn)

        conn.execute(MIGRATION_0028.read_text())
        conn.commit()

        assert _keys_by_name(conn) == dict(LEGACY_ROWS)


def test_migration_0028_applies_the_same_guards_as_the_runtime_repair() -> None:
    with migrated_schema() as conn:
        # An admin-set key and a key already held elsewhere — neither
        # may be touched or duplicated.
        conn.execute(
            "INSERT INTO sources (name, method, registry_key, enabled) VALUES (%s, %s, %s, %s)",
            ("Prothom Alo", "rss", "daily_star", True),
        )
        conn.execute(
            "INSERT INTO sources (name, method, registry_key, enabled) VALUES (%s, %s, %s, %s)",
            ("Sangbad (renamed)", "rss", "sangbad", True),
        )
        conn.execute("INSERT INTO sources (name, method, enabled) VALUES (%s, %s, %s)", ("Sangbad", "rss", True))
        conn.commit()

        conn.execute(MIGRATION_0028.read_text())
        conn.commit()

        rows = conn.execute("SELECT name, registry_key FROM sources").fetchall()
        keys = [(r["name"], r["registry_key"]) for r in rows]
        assert sorted(keys) == sorted(
            [
                ("Prothom Alo", "daily_star"),
                ("Sangbad (renamed)", "sangbad"),
                ("Sangbad", None),
            ]
        )


def test_migration_0028_pairs_match_the_live_plugin_registry() -> None:
    """The migration's VALUES list was generated from
    `app.sources.registry`; if a plugin is renamed or added, this fails
    rather than letting the two drift apart silently. Note the runtime
    repair reads the registry directly and therefore cannot drift — this
    only pins the frozen copy inside the SQL.
    """
    load_all()
    live = {getattr(cls, "display_name", key): key for key, cls in all_sources().items()}
    sql = MIGRATION_0028.read_text()
    in_sql = dict(
        line.strip().rstrip(",").strip("()").replace("'", "").split(", ")  # noqa: B005 - fixed two-field rows
        for line in sql.splitlines()
        if line.startswith("    ('")
    )
    in_sql = {name.strip(): key.strip() for name, key in in_sql.items()}
    assert in_sql == live

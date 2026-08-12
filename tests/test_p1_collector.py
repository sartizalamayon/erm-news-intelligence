"""app.services.collector — two-tier bounded fan-out, per-source
isolation (GATE 2: zero unhandled exceptions), and the dedup-on-insert
write path. Uses hand-rolled FAKE `NewsSource` subclasses (never the 21
real, network-touching ones) and a FAKE `queries` module stand-in (never
a real DB connection) — fully deterministic, no network, no DB.

One exception, at the bottom: a `pg_available()`-gated test proving
`_filter_enabled`'s DB `sources.enabled` exclusion against a real,
freshly migrated Postgres schema (app.db.testing.migrated_schema), not
just a hand-rolled conn double — still zero network (the fake
NewsSource subclasses above never touch the network either way), just
a real DB round trip for the one query `_filter_enabled` issues.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.db.testing import migrated_schema, pg_available
from app.errors import SourceFetchError
from app.services import collector
from app.sources.base import FeedItem, FetchedArticle, NewsSource


def _item(url: str, title: str = "Title") -> FeedItem:
    return FeedItem(url=url, title=title, source_key="fake", published_at=datetime.now(timezone.utc))


class OkSource(NewsSource):
    key = "ok_source"
    display_name = "OK Source"

    def iter_feed_items(self) -> list[FeedItem]:
        return [_item("https://ok.example/a"), _item("https://ok.example/b")]

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="")


class EmptySource(NewsSource):
    key = "empty_source"
    display_name = "Empty Source"

    def iter_feed_items(self) -> list[FeedItem]:
        return []

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="")


class FailingSource(NewsSource):
    key = "failing_source"
    display_name = "Failing Source"

    def iter_feed_items(self) -> list[FeedItem]:
        raise SourceFetchError("host is down", source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="")


class BuggySource(NewsSource):
    """Simulates a genuine bug — NOT a modeled SourceError. Per module
    docstring, this must still be isolated (recorded failed), never
    crash the whole run.
    """

    key = "buggy_source"
    display_name = "Buggy Source"

    def iter_feed_items(self) -> list[FeedItem]:
        raise ValueError("someone forgot a null check")

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="")


class DuplicateWithinSource(NewsSource):
    key = "dup_source"
    display_name = "Dup Source"

    def iter_feed_items(self) -> list[FeedItem]:
        return [_item("https://dup.example/same"), _item("https://dup.example/same")]

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="")


class FakeQueries:
    """Stands in for app.db.queries — an in-memory articles table
    faithful to the real ON CONFLICT (url_norm) DO NOTHING semantics,
    without a real DB connection.
    """

    def __init__(self) -> None:
        self.seen_urls: set[str] = set()
        self.rows: list[dict] = []
        self._next_id = 1
        self.heartbeat_calls: list[int] = []

    def url_norm_seen(self, conn, url_norm: str) -> bool:
        return url_norm in self.seen_urls

    def insert_article(self, conn, **fields):
        url_norm = fields.get("url_norm")
        if any(r["url"] == fields.get("url") for r in self.rows):
            return None
        if url_norm and any(r.get("url_norm") == url_norm for r in self.rows):
            return None
        row_id = self._next_id
        self._next_id += 1
        self.rows.append({**fields, "id": row_id})
        return row_id

    def remember_seen_url(self, conn, url_norm: str, first_seen_day) -> None:
        self.seen_urls.add(url_norm)

    def touch_run_heartbeat(self, conn, run_id: int, at=None) -> None:
        # Real signature per app/db/queries.py — collector._touch_heartbeat
        # calls this the same way whether `queries` is real or faked.
        self.heartbeat_calls.append(run_id)


@pytest.fixture
def fake_queries(monkeypatch):
    fq = FakeQueries()
    monkeypatch.setattr(collector, "queries", fq)
    return fq


class _FakeCursor:
    def __init__(self, row: dict | None, rows: list[dict] | None = None) -> None:
        self._row = row
        # Only `_filter_enabled`'s "SELECT registry_key FROM sources
        # WHERE ... enabled = FALSE" query calls `.fetchall()` (the
        # `runs`-table INSERT/UPDATE branches below only ever call
        # `.fetchone()`); FakeConn's catch-all falls through to
        # `_FakeCursor(None)`, so an empty default here means "the DB
        # has no opinion" -- no source_classes entry gets excluded,
        # which is exactly right for a conn double with no real
        # `sources` table behind it.
        self._rows = rows if rows is not None else []

    def fetchone(self) -> dict | None:
        return self._row

    def fetchall(self) -> list[dict]:
        return self._rows


class FakeConn:
    """Minimal stand-in for `DictConnection`, sufficient for
    `run_collection()`'s own direct run-lifecycle SQL (`INSERT INTO
    runs ... RETURNING id`, `UPDATE runs ...` — RT-1's fix,
    docs/red-team-findings.md). The article-write path never touches
    this directly (it goes through the monkeypatched `queries` module
    above); only `_start_run`/`_finalize_run` issue raw `conn.execute()`
    calls, since `app/db/queries.py` (ARCH-DB's file) doesn't have
    `runs`-table helpers to fake against yet.
    """

    def __init__(self) -> None:
        self._next_run_id = 1
        self.commit_count = 0
        self.executed: list[tuple[str, object]] = []
        self.runs: dict[int, dict] = {}

    def execute(self, sql: str, params: object = None) -> _FakeCursor:
        self.executed.append((sql, params))
        normalized = " ".join(sql.split())
        if normalized.startswith("INSERT INTO runs"):
            run_id = self._next_run_id
            self._next_run_id += 1
            trigger = params[0] if params else "manual"
            self.runs[run_id] = {"id": run_id, "status": "running", "trigger": trigger}
            return _FakeCursor({"id": run_id})
        if normalized.startswith("UPDATE runs"):
            # params order matches collector._finalize_run's UPDATE:
            # (status, total_new, total_matched, details, run_id)
            status, total_new, total_matched, details, run_id = params  # type: ignore[misc]
            self.runs.setdefault(run_id, {})
            self.runs[run_id].update(
                status=status, total_new=total_new, total_matched=total_matched, details=details
            )
            return _FakeCursor(None)
        return _FakeCursor(None)

    def commit(self) -> None:
        self.commit_count += 1


# ── fetch_all: fan-out + isolation ──────────────────────────────────────


def test_fetch_all_runs_every_source_and_isolates_failures():
    sources = {
        "ok_source": OkSource,
        "empty_source": EmptySource,
        "failing_source": FailingSource,
        "buggy_source": BuggySource,
    }
    results = collector.fetch_all(sources, max_workers=4)
    assert set(results) == set(sources)

    ok_result, ok_items = results["ok_source"]
    assert ok_result.status == "ok"
    assert len(ok_items) == 2

    empty_result, empty_items = results["empty_source"]
    assert empty_result.status == "empty"
    assert empty_items == []

    failing_result, failing_items = results["failing_source"]
    assert failing_result.status == "failed"
    assert "host is down" in failing_result.error
    assert failing_items == []

    buggy_result, buggy_items = results["buggy_source"]
    assert buggy_result.status == "failed"
    assert "unexpected" in buggy_result.error
    assert buggy_items == []


def test_one_source_failing_does_not_prevent_others_from_succeeding():
    """The core GATE 2 guarantee, stated as a direct assertion: a
    malformed/dead source must never abort the run or lose sources that
    already succeeded.
    """
    sources = {"ok_source": OkSource, "failing_source": FailingSource, "buggy_source": BuggySource}
    results = collector.fetch_all(sources)
    assert results["ok_source"][0].status == "ok"
    assert len(results["ok_source"][1]) == 2


def test_fetch_all_with_empty_registry_returns_empty():
    assert collector.fetch_all({}) == {}


def test_fetch_all_default_loads_real_registry(monkeypatch):
    """When no explicit source_classes is passed, fetch_all() loads the
    real registry — proven WITHOUT touching the network by swapping in
    fakes at the registry level.
    """
    monkeypatch.setattr(collector, "all_sources", lambda: {"ok_source": OkSource})
    monkeypatch.setattr(collector, "load_all", lambda: None)
    results = collector.fetch_all()
    assert set(results) == {"ok_source"}


# ── write_items: dedup-on-insert ────────────────────────────────────────


def test_write_items_inserts_new_articles(fake_queries):
    result = collector.SourceResult(source_key="ok_source", display_name="OK Source", status="ok", fetched=2)
    items = [_item("https://x.example/1", "One"), _item("https://x.example/2", "Two")]
    updated = collector.write_items(_conn(), result, items, day=date(2026, 8, 6))
    assert updated.inserted == 2
    assert updated.duplicate == 0
    assert len(fake_queries.rows) == 2


def test_write_items_dedupes_within_the_same_batch(fake_queries):
    result = collector.SourceResult(source_key="dup_source", display_name="Dup Source", status="ok", fetched=2)
    items = [_item("https://dup.example/same", "A"), _item("https://dup.example/same", "A dup")]
    updated = collector.write_items(_conn(), result, items, day=date(2026, 8, 6))
    assert updated.inserted == 1
    assert updated.duplicate == 1


def test_write_items_dedupes_against_seen_urls(fake_queries):
    from app.sources._util import normalize_url

    fake_queries.seen_urls.add(normalize_url("https://already.example/seen"))
    result = collector.SourceResult(source_key="s", display_name="S", status="ok", fetched=1)
    items = [_item("https://already.example/seen", "Old news")]
    updated = collector.write_items(_conn(), result, items, day=date(2026, 8, 6))
    assert updated.inserted == 0
    assert updated.duplicate == 1
    assert fake_queries.rows == []


def test_write_items_remembers_newly_inserted_url_norm(fake_queries):
    from app.sources._util import normalize_url

    result = collector.SourceResult(source_key="s", display_name="S", status="ok", fetched=1)
    items = [_item("https://new.example/story", "New")]
    collector.write_items(_conn(), result, items, day=date(2026, 8, 6))
    assert normalize_url("https://new.example/story") in fake_queries.seen_urls


def test_write_items_uses_display_name_as_source_column(fake_queries):
    result = collector.SourceResult(source_key="dhaka_post", display_name="Dhaka Post", status="ok", fetched=1)
    items = [_item("https://dhakapost.example/1", "Story")]
    collector.write_items(_conn(), result, items, day=date(2026, 8, 6))
    assert fake_queries.rows[0]["source"] == "Dhaka Post"


# ── run_collection: end-to-end wiring ───────────────────────────────────


def test_run_collection_with_conn_none_does_not_touch_queries(fake_queries):
    sources = {"ok_source": OkSource}
    result = collector.run_collection(None, source_classes=sources)
    assert result.total_fetched == 2
    assert result.total_inserted == 0  # fetch-only, nothing written
    assert fake_queries.rows == []


def test_run_collection_with_conn_writes_and_tallies(fake_queries):
    sources = {"ok_source": OkSource, "empty_source": EmptySource, "failing_source": FailingSource}
    conn = _conn()
    result = collector.run_collection(conn, source_classes=sources)
    assert result.total_fetched == 2
    assert result.total_inserted == 2
    assert result.total_failed_sources == 1
    assert result.total_empty_sources == 1
    assert len(result.sources) == 3


# ── run_collection: runs-table lifecycle (RT-1, docs/red-team-findings.md) ──


def test_run_collection_creates_and_finalizes_a_runs_row(fake_queries):
    sources = {"ok_source": OkSource}
    conn = _conn()
    result = collector.run_collection(conn, source_classes=sources)

    assert len(conn.runs) == 1
    run = next(iter(conn.runs.values()))
    assert run["status"] == "done"
    assert run["total_new"] == result.total_inserted == 2
    assert run["total_matched"] == 0  # P2's job, not P1's — see module docstring
    details = run["details"].obj  # psycopg.types.json.Jsonb wrapper — .obj is the real dict
    assert isinstance(details, dict)
    assert "OK Source" in details


def test_run_collection_touches_heartbeat_per_completed_source(fake_queries):
    sources = {"ok_source": OkSource, "empty_source": EmptySource, "failing_source": FailingSource}
    conn = _conn()
    collector.run_collection(conn, source_classes=sources)
    assert len(fake_queries.heartbeat_calls) == 3  # once per source, regardless of status


def test_run_collection_finalizes_as_error_when_it_crashes(fake_queries, monkeypatch):
    """A bug inside run_collection's OWN body (not a per-source failure,
    which is already isolated) must still leave a `status='error'` row
    behind, not an eternally-'running' one, and must still propagate the
    exception (this is a real bug surfacing loudly, not something to
    swallow).
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated bug inside run_collection")

    monkeypatch.setattr(collector, "fetch_all", _boom)
    conn = _conn()
    with pytest.raises(RuntimeError, match="simulated bug"):
        collector.run_collection(conn, source_classes={"ok_source": OkSource})

    assert len(conn.runs) == 1
    run = next(iter(conn.runs.values()))
    assert run["status"] == "error"


def test_run_collection_with_conn_none_never_touches_runs_table(fake_queries):
    """Fetch-only mode (no DATABASE_URL) has no DB to track a run in —
    must not attempt any runs-table SQL at all.
    """
    result = collector.run_collection(None, source_classes={"ok_source": OkSource})
    assert result.total_fetched == 2
    assert fake_queries.heartbeat_calls == []


def _conn() -> FakeConn:
    """A conn double sufficient for both the monkeypatched `queries`
    module (article writes — FakeQueries never actually dereferences the
    conn it's passed, it only appears as the first positional arg to
    mirror the real app.db.queries function signatures) AND
    run_collection()'s own direct `runs`-table SQL (FakeConn, RT-1).
    """
    return FakeConn()


# ── _filter_enabled: bug fix, DB `sources.enabled` actually excludes ────
# a source from the fetch set (previously nothing checked it at all —
# see collector._filter_enabled's own docstring for the full writeup).


class _StubSourcesConn:
    """Just enough of `DictConnection` for `_filter_enabled`'s one query
    — never touches the `runs` table, so it's deliberately NOT a
    `FakeConn` (keeps this test from depending on run-lifecycle
    plumbing it has nothing to do with).
    """

    def __init__(self, disabled_registry_keys: set[str]) -> None:
        self._disabled = disabled_registry_keys

    def execute(self, sql: str, params: object = None) -> _FakeCursor:
        return _FakeCursor(None, rows=[{"registry_key": k} for k in self._disabled])


def test_filter_enabled_excludes_only_keys_the_db_marks_disabled() -> None:
    classes = {"ok_source": OkSource, "empty_source": EmptySource, "failing_source": FailingSource}
    filtered = collector._filter_enabled(_StubSourcesConn({"empty_source"}), classes)
    assert set(filtered) == {"ok_source", "failing_source"}


def test_filter_enabled_is_a_noop_when_nothing_is_disabled() -> None:
    classes = {"ok_source": OkSource, "empty_source": EmptySource}
    filtered = collector._filter_enabled(_StubSourcesConn(set()), classes)
    assert filtered == classes


def test_filter_enabled_leaves_untracked_registry_keys_alone() -> None:
    """A registry key with no matching `sources` row at all (e.g. a test
    double's conn, or a real DB where that key was never seeded) is not
    an implicit disable — silence isn't a disable vote.
    """
    classes = {"ok_source": OkSource}
    filtered = collector._filter_enabled(_StubSourcesConn({"some_other_key"}), classes)
    assert filtered == classes


class _FakeConnWithDisabledSources(FakeConn):
    """FakeConn plus one disabled registry key, so
    `run_collection`'s `_filter_enabled` call sees a non-empty result —
    everything else (the `runs`-table INSERT/UPDATE handling) is
    inherited unchanged from `FakeConn`.
    """

    def __init__(self, disabled_registry_keys: set[str]) -> None:
        super().__init__()
        self._disabled = disabled_registry_keys

    def execute(self, sql: str, params: object = None) -> _FakeCursor:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT registry_key FROM sources"):
            return _FakeCursor(None, rows=[{"registry_key": k} for k in self._disabled])
        return super().execute(sql, params)


def test_run_collection_with_conn_applies_db_enabled_filter(fake_queries):
    """End-to-end through `run_collection` (not just `_filter_enabled`
    directly): a conn standing in for a real `sources` table with one
    disabled key must exclude that source from what actually gets
    fetched and written, while an explicitly-passed sibling with no
    opinion recorded runs as normal.
    """
    conn = _FakeConnWithDisabledSources({"empty_source"})
    sources = {"ok_source": OkSource, "empty_source": EmptySource}
    result = collector.run_collection(conn, source_classes=sources)
    ran_keys = {s.source_key for s in result.sources}
    assert ran_keys == {"ok_source"}


# ── _filter_enabled against a real, migrated Postgres schema ────────────
# (Bug 1's actual production shape: `sources.enabled = FALSE` set via
# the admin API, correlated by `sources.registry_key` — migrations/
# 0014_sources_registry_key.sql — must exclude that source from a real
# `run_collection` call, not just from a hand-rolled stub conn above.)


@pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")
def test_run_collection_excludes_a_source_disabled_via_the_real_sources_table() -> None:
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO sources (name, method, registry_key, enabled) VALUES (%s, %s, %s, %s)",
            ("Disabled Outlet", "rss", "off_source", False),
        )
        conn.execute(
            "INSERT INTO sources (name, method, registry_key, enabled) VALUES (%s, %s, %s, %s)",
            ("Enabled Outlet", "rss", "on_source", True),
        )
        conn.commit()

        result = collector.run_collection(
            conn,
            source_classes={"off_source": OkSource, "on_source": EmptySource},
            max_workers=1,
        )

        ran_keys = {s.source_key for s in result.sources}
        assert "on_source" in ran_keys
        assert "off_source" not in ran_keys

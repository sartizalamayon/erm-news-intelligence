"""app.services.collector — two-tier bounded fan-out, per-source
isolation (GATE 2: zero unhandled exceptions), and the dedup-on-insert
write path. Uses hand-rolled FAKE `NewsSource` subclasses (never the 21
real, network-touching ones) and a FAKE `queries` module stand-in (never
a real DB connection) — fully deterministic, no network, no DB.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

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


@pytest.fixture
def fake_queries(monkeypatch):
    fq = FakeQueries()
    monkeypatch.setattr(collector, "queries", fq)
    return fq


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
    result = collector.run_collection(_conn(), source_classes=sources)
    assert result.total_fetched == 2
    assert result.total_inserted == 2
    assert result.total_failed_sources == 1
    assert result.total_empty_sources == 1
    assert len(result.sources) == 3


def _conn():
    """A placeholder conn object — FakeQueries never actually dereferences
    it (it only appears as the first positional arg to mirror the real
    app.db.queries function signatures), so any sentinel works.
    """
    return object()

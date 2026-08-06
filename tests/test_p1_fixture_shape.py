"""Validates that P1-FETCH's own output conforms EXACTLY to the frozen
`p1_raw` contract test-strategy.md §1.2 documents and
`fixtures/p1_raw_20260806.json` already embodies (produced by ARCH-TEST's
`tests/fixtures/build_fixtures.py` ahead of this task, from real historical
`data/news.db` rows — see the P1-FETCH task report for why that existing
fixture is treated as satisfying DoD #6 rather than being regenerated
here). This file is the missing link: it proves a REAL `FeedItem` +
`day`/`source` produced by this task's own code, once written to the
`articles` row shape, has exactly the same keys as that committed
fixture — not a re-statement of the same JSON, an independent structural
check against real code.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from app.services.collector import SourceResult, write_items
from app.sources._util import normalize_url
from app.sources.base import FeedItem
from tests.fixtures.loaders import load_p1_raw

P1_RAW_CONTRACT_COLUMNS = {"url", "url_norm", "source", "title", "published", "day"}


class FakeQueries:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.seen: set[str] = set()
        self._next_id = 1

    def url_norm_seen(self, conn, url_norm):
        return url_norm in self.seen

    def insert_article(self, conn, **fields):
        row_id = self._next_id
        self._next_id += 1
        self.rows.append({**fields, "id": row_id})
        return row_id

    def remember_seen_url(self, conn, url_norm, first_seen_day):
        self.seen.add(url_norm)


def test_committed_p1_raw_fixture_matches_the_documented_contract_columns():
    """Sanity check on the ALREADY-COMMITTED fixture this task inherits
    (fixtures/p1_raw_20260806.json) — every row has exactly the
    documented columns, nothing from P2 (`sections`/`keywords`/
    `review_status`) leaked in.
    """
    rows = load_p1_raw()["rows"]
    assert rows, "fixture must be non-empty for this check to mean anything"
    for row in rows:
        row_keys = set(row) - {"id"}  # `id` is the DB PK, not part of P1's own contract
        assert row_keys == P1_RAW_CONTRACT_COLUMNS, row


def test_my_own_write_path_produces_the_same_column_shape(monkeypatch):
    """Runs a REAL FeedItem through THIS task's write_items() (with a
    fake queries backend, no DB) and confirms the fields passed to
    insert_article are exactly the P1 contract columns — i.e. my
    collector never accidentally tries to write a P2 field
    (sections/keywords/review_status) itself, matching the module
    docstring's stated scope boundary.
    """
    fake_queries = FakeQueries()
    import app.services.collector as collector_module

    monkeypatch.setattr(collector_module, "queries", fake_queries)

    item = FeedItem(
        url="https://example.com/a-real-story",
        title="A Real Story Headline",
        source_key="daily_inqilab",
        published_at=datetime(2026, 8, 6, 9, 0, tzinfo=timezone.utc),
    )
    result = SourceResult(source_key="daily_inqilab", display_name="Daily Inqilab", status="ok", fetched=1)
    write_items(object(), result, [item], day=date(2026, 8, 6))

    assert len(fake_queries.rows) == 1
    written = fake_queries.rows[0]
    written_keys = set(written) - {"id"}  # FakeQueries' own bookkeeping, not a field write_items() passed
    assert written_keys == P1_RAW_CONTRACT_COLUMNS
    assert written["url"] == item.url
    assert written["url_norm"] == normalize_url(item.url)
    assert written["source"] == "Daily Inqilab"
    assert written["title"] == item.title
    assert written["published"] == item.published_at
    assert written["day"] == date(2026, 8, 6)


def test_undated_article_is_still_written_with_published_none():
    """legacy-behavior.md §2.5: dt is None "when the source genuinely
    can't supply one" — that must round-trip as a NULL `published`, not
    an error or a dropped row (P1 keeps undated articles; only the
    window filter inside each method-family fetcher drops confidently-
    OLD ones, never confidently-unknown ones).
    """
    fake_queries = FakeQueries()

    class _Conn:
        pass

    item = FeedItem(url="https://example.com/undated", title="No date available", source_key="x", published_at=None)
    result = SourceResult(source_key="x", display_name="X", status="ok", fetched=1)

    import app.services.collector as collector_module

    original_queries = collector_module.queries
    collector_module.queries = fake_queries
    try:
        write_items(_Conn(), result, [item], day=date(2026, 8, 6))
    finally:
        collector_module.queries = original_queries

    assert fake_queries.rows[0]["published"] is None

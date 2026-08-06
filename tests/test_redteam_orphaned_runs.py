"""RED-TEAM: mid-run crash / orphaned `runs` rows (mission attack surface
"Mid-run crashes" — legacy-behavior.md §3/§7 item 10: 16 of 110 legacy
runs are permanently stuck at `status='running'` with no `finished_at`,
because nothing in the legacy app ever swept them).

migrations/0002_reference_tables.sql was built specifically to fix this:
a `runs` table with `heartbeat_at`, an `idx_runs_status_running` partial
index, and `app.db.queries.touch_run_heartbeat()` — its own comments
describe the intended watchdog query ("find runs stuck at running") and
call out the exact legacy defect this exists to close.

Finding (see docs/red-team-findings.md, RT-1): nothing in app/ ever
calls any of this. `app.batch.run_daily.main()` — the actual, only batch
entrypoint (app.batch.__main__ imports it, the CI "Batch entrypoint smoke
check" step runs it, ADR §1 wires it to the daily GitHub Actions cron) —
never INSERTs a `runs` row, never calls `touch_run_heartbeat`, and never
marks a run 'done' or 'error'. Grepped across app/: the only callers of
`touch_run_heartbeat` anywhere in the codebase are its own definition and
docstring in app/db/queries.py. `app.services.collector.run_collection`
(the function run_daily.main() calls) never touches the `runs` table
either.

Net effect: it's not that the rebuild reduced legacy's 16/110 (14.5%)
stuck-run rate — it's that 100% of runs today are invisible. There is no
row to get stuck, but there is also no row to prove a run happened,
succeeded, or crashed at all. The watchdog schema exists and is fully
inert.

Postgres-gated (pg_available()), matching the pattern every other
DB-backed test in this repo uses (tests/test_auth_api.py Tier 2,
tests/test_storage.py, ...).
"""

from __future__ import annotations

import pytest

from app.batch.run_daily import main as batch_main
from app.db.testing import migrated_schema, pg_available
from app.services.collector import run_collection
from app.sources.base import FeedItem, FetchedArticle, NewsSource

pg_gated = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


class _OneArticleSource(NewsSource):
    key = "rt_one"
    display_name = "RT One"

    def iter_feed_items(self) -> list[FeedItem]:
        return [FeedItem(url="https://rt.example/a", title="A", source_key=self.key)]

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="")


def _runs_row_count(conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM runs").fetchone()
    return int(row["n"]) if row else 0


@pg_gated
@pytest.mark.xfail(
    strict=True,
    reason="RT-1 (docs/red-team-findings.md): app.services.collector.run_collection never "
    "writes a `runs` row at all — the ARCH-DB watchdog schema (heartbeat_at, "
    "idx_runs_status_running, touch_run_heartbeat) is completely unwired. Owner: P1-INGEST.",
)
def test_run_collection_records_a_runs_row() -> None:
    with migrated_schema() as conn:
        before = _runs_row_count(conn)
        run_collection(conn, source_classes={"rt_one": _OneArticleSource}, max_workers=1)
        after = _runs_row_count(conn)
        assert after == before + 1, (
            f"expected run_collection() to insert exactly one `runs` row (before={before}, after={after})"
        )


@pg_gated
@pytest.mark.xfail(
    strict=True,
    reason="RT-1 (docs/red-team-findings.md): app.batch.run_daily.main(), the ONLY real batch "
    "entrypoint (wired to the daily cron per ADR §1), never creates or finalizes a `runs` row "
    "for the run it just performed. A process kill mid-run leaves no trace at all -- not even "
    "a 'running' row to eventually reap. Owner: P1-INGEST.",
)
def test_batch_main_records_a_runs_row(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        from contextlib import contextmanager

        import app.batch.run_daily as run_daily_module
        import app.db.connection as connection_module

        @contextmanager
        def _fake_get_connection(dsn: str | None = None):
            yield conn

        monkeypatch.setattr(connection_module, "get_connection", _fake_get_connection)
        monkeypatch.setattr(run_daily_module, "get_connection", _fake_get_connection)
        monkeypatch.setattr(run_daily_module, "all_sources", lambda: {"rt_one": _OneArticleSource})
        monkeypatch.setattr(run_daily_module, "load_all", lambda: None)
        monkeypatch.setattr(
            run_daily_module,
            "get_settings",
            lambda: type("S", (), {"app_env": "test", "database_url": "postgresql:///fake"})(),
        )
        monkeypatch.setattr(run_daily_module, "validate_production_settings", lambda settings: None)

        before = _runs_row_count(conn)
        assert batch_main() == 0
        after = _runs_row_count(conn)
        assert after == before + 1, (
            f"expected a `runs` row to exist after a full batch run (before={before}, after={after})"
        )

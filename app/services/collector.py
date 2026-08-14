"""The P1 fetch-layer orchestrator: two-tier bounded fan-out across every
registered source, per-source failure isolation, dedup-on-insert write.

Scope note (mission boundary): this module implements P1 — fetch,
dedup, write raw article rows. It deliberately does NOT compute
`sections`/`keywords`/`review_status` (P2's keyword-match output,
legacy-behavior.md §4) — that is `app/services/matching.py`'s job,
owned by the concurrent P2-MATCH task. Rows land here with the schema's
own defaults (`review_status='pending'`, `sections=''`, `keywords=''`,
`match_type=''`) and P2 updates them in a separate pass. This mirrors
`fixtures/p1_raw_*.json`'s documented shape (test-strategy.md §1.2):
`url`, `url_norm`, `source`, `title`, `published`, `day` — nothing from
P2 or later.

Two-tier fan-out (speed-techniques.md §4 point 3):
  Tier 1 — sources run concurrently (`fetch_all`'s ThreadPoolExecutor,
    `SOURCE_PARALLELISM` wide).
  Tier 2 — "articles within a source in parallel" lives INSIDE each
    source's own `iter_feed_items()` for the strategies that need it
    (id_enum, daily_star_nodes each run their own bounded
    ThreadPoolExecutor — see app/sources/_id_enum.py,
    app/sources/_daily_star.py). Both tiers are further capped by
    `app.sources.http.GLOBAL_SEMAPHORE`, a process-wide ceiling on total
    in-flight HTTP requests regardless of which tier or which source
    made the call.

Per-source isolation (speed-techniques.md §5, GATE 2): a `SourceError`
from one source's `iter_feed_items()` is caught, logged, and recorded as
that source's own `failed` status — every other source's future
continues independently. A genuinely UNEXPECTED exception (a bug, not a
modeled failure — see app/errors.py's `AppError` docstring) is also
caught at this boundary and recorded as `failed`, rather than crashing
the whole batch: for a once-daily unattended batch job, one buggy source
silently losing its own results is a strictly better failure mode than
losing every OTHER source's already-fetched data too. It is never
swallowed silently — every case is logged with the exception type and
message.

Run lifecycle (`runs` table, migrations/0002_reference_tables.sql —
fixes docs/red-team-findings.md RT-1: "every collection run is
invisible; nothing ever writes to `runs`"): `run_collection()` inserts a
`runs` row before doing any fetch work, commits it IMMEDIATELY
(independent of the article-write transaction — a crash one line later
must still leave a durable `status='running'` row for a watchdog to
find, which an uncommitted INSERT cannot provide), touches
`heartbeat_at` (via `app.db.queries.touch_run_heartbeat`, committed
each time for the same reason) once per completed source, and finalizes
the row — `status='done'` or `status='error'`, with `total_new` and a
per-source `details` JSONB blob — on BOTH the success path and any
exception that survives per-source isolation. `app/db/queries.py` does
not yet have `INSERT INTO runs`/`UPDATE runs` helpers (only
`touch_run_heartbeat`, which this module already uses); the two other
statements are small enough, and specific enough to this module's own
lifecycle, to live here rather than block on an ARCH-DB-owned file this
task cannot edit.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from psycopg.types.json import Jsonb

from app.db import queries
from app.db.connection import DictConnection
from app.errors import SourceError
from app.logging import get_logger
from app.sources._util import normalize_url, today_dhaka
from app.sources.base import FeedItem, NewsSource
from app.sources.registry import all_sources, load_all

logger = get_logger(__name__)

SOURCE_PARALLELISM = 8  # tier 1: sources fetched concurrently


@dataclass
class SourceResult:
    source_key: str
    display_name: str
    status: str  # "ok" | "empty" | "failed"
    fetched: int = 0
    inserted: int = 0
    duplicate: int = 0
    error: str | None = None
    # Wall-clock time this source's own iter_feed_items() call took
    # (discovery only, not the write path) — populated by
    # _run_one_source. Exists so a live-measurement run can report
    # per-source timings / identify the slowest sources (GATE 2) without
    # a separate ad-hoc timing harness.
    duration_seconds: float = 0.0


@dataclass
class CollectionRunResult:
    sources: list[SourceResult] = field(default_factory=list)
    total_fetched: int = 0
    total_inserted: int = 0
    total_duplicate: int = 0
    # Wall-clock for the whole run (fetch_all's fan-out + any DB write),
    # set by run_collection. 0.0 if the caller never populated it (e.g.
    # a test that only exercises fetch_all directly).
    wall_seconds: float = 0.0
    # ADMIN-API's manual "run collection now" trigger (app.services.admin
    # .start_fetch_run): the `runs.id` this call created, so a caller
    # that wants to correlate this result with the runs-table row (e.g.
    # GET /health/runs) doesn't have to re-query for "the latest run" and
    # risk a race against a second concurrent trigger. `None` when
    # `conn=None` (fetch-only, no runs row exists at all) — additive
    # field, existing callers that never read it are unaffected.
    run_id: int | None = None

    @property
    def total_failed_sources(self) -> int:
        return sum(1 for s in self.sources if s.status == "failed")

    @property
    def total_empty_sources(self) -> int:
        return sum(1 for s in self.sources if s.status == "empty")

    def slowest(self, n: int = 5) -> list[SourceResult]:
        return sorted(self.sources, key=lambda s: s.duration_seconds, reverse=True)[:n]


def _display_name(source_cls: type[NewsSource]) -> str:
    return getattr(source_cls, "display_name", source_cls.key)


def _run_one_source(key: str, source_cls: type[NewsSource]) -> tuple[SourceResult, list[FeedItem]]:
    """Instantiate and run exactly one source's discovery. Never raises —
    every failure mode (modeled `SourceError` or a genuine bug) is
    converted into a `failed` `SourceResult` here, at the innermost
    possible boundary, so `fetch_all`'s executor loop never has to
    special-case which future blew up.
    """
    name = _display_name(source_cls)
    start = time.monotonic()
    try:
        source = source_cls()
        items = source.iter_feed_items()
    except SourceError as exc:
        elapsed = time.monotonic() - start
        logger.warning(
            "source_fetch_failed",
            extra={"source": key, "error_type": type(exc).__name__, "error": str(exc), "duration_seconds": elapsed},
        )
        return (
            SourceResult(
                source_key=key, display_name=name, status="failed", error=str(exc), duration_seconds=elapsed
            ),
            [],
        )
    except Exception as exc:  # deliberately broad — see module docstring
        elapsed = time.monotonic() - start
        logger.error(
            "source_run_unexpected_exception",
            extra={"source": key, "error_type": type(exc).__name__, "error": str(exc), "duration_seconds": elapsed},
        )
        return (
            SourceResult(
                source_key=key,
                display_name=name,
                status="failed",
                error=f"unexpected: {exc}",
                duration_seconds=elapsed,
            ),
            [],
        )

    elapsed = time.monotonic() - start
    if not items:
        logger.warning("source_returned_zero_items", extra={"source": key, "duration_seconds": elapsed})
        return SourceResult(source_key=key, display_name=name, status="empty", duration_seconds=elapsed), []
    return (
        SourceResult(source_key=key, display_name=name, status="ok", fetched=len(items), duration_seconds=elapsed),
        items,
    )


def fetch_all(
    source_classes: dict[str, type[NewsSource]] | None = None,
    *,
    max_workers: int = SOURCE_PARALLELISM,
    on_result: Callable[[str, SourceResult, list[FeedItem]], None] | None = None,
) -> dict[str, tuple[SourceResult, list[FeedItem]]]:
    """Tier 1 of the two-tier fan-out. `source_classes=None` loads and
    uses the full registry; pass an explicit dict (including `{}`) to
    run a subset — the mechanism `tests/test_p1_collector.py` and the
    live-measurement script both use to avoid hardcoding the registry.

    `on_result`, if given, is called once per source IMMEDIATELY as that
    source's future resolves (inside the `as_completed` loop below, in
    the calling thread — never from a worker thread, so a callback that
    touches a single non-thread-safe `DictConnection`, as
    `run_collection`'s does, is safe). This is what turns "45 sources
    fetched concurrently, nothing observable until the slowest one
    finishes" into genuine incremental progress: `results[key]` is still
    populated for every caller that only wants the final dict (e.g.
    tests, the live-measurement script), but a caller that ALSO wants to
    know as-it-happens which source just finished (ADMIN-API's manual
    "run collection now" trigger, run_collection's `progress_cb` below)
    no longer has to wait for every one of the ~45 futures to resolve
    before seeing the first one.
    """
    if source_classes is None:
        load_all()
        source_classes = all_sources()

    results: dict[str, tuple[SourceResult, list[FeedItem]]] = {}
    if not source_classes:
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_one_source, key, cls): key for key, cls in source_classes.items()}
        for future in as_completed(futures):
            key = futures[future]
            source_result, items = future.result()
            results[key] = (source_result, items)
            if on_result is not None:
                on_result(key, source_result, items)
    return results


def write_items(conn: DictConnection, source_result: SourceResult, items: list[FeedItem], *, day: date) -> SourceResult:
    """Dedup-on-insert write path (legacy-behavior.md §2.6 minus P2's
    match step). Two-step dedup, per app/db/queries.py's own docstring:

    1. `seen_urls` first — catches a URL whose `articles` row no longer
       exists (aged out of the retention window, or purged via the
       manual release valve), which the live `articles.url_norm` index
       alone cannot catch.
    2. `insert_article()`'s own `ON CONFLICT (url_norm) DO NOTHING`
       against currently-live rows — a single indexed lookup baked into
       the INSERT itself, never the legacy `WHERE url=? OR url_norm=?`
       full-table scan (ADR §2, the single worst bottleneck in the old
       system — explicitly not reintroduced here).

    Note what this function no longer does: it does NOT write to
    `seen_urls` on a successful insert. `seen_urls` is a tombstone table
    maintained by the `articles_remember_deleted_urls` trigger
    (migrations/0016) at the only moment its contents mean anything — a
    row ceasing to exist. Writing it here as well used to duplicate
    every live row into it, and, because several delete paths never
    cleaned it up, left orphaned entries that permanently blocked a URL
    from ever being re-collected (8,931 of them, measured live). See
    0016's header for the full post-mortem.
    """
    inserted = 0
    duplicate = 0
    for item in items:
        url_norm = normalize_url(item.url)
        if url_norm and queries.url_norm_seen(conn, url_norm):
            duplicate += 1
            continue
        new_id = queries.insert_article(
            conn,
            url=item.url,
            url_norm=url_norm,
            source=source_result.display_name,
            title=item.title,
            published=item.published_at,
            day=day,
        )
        if new_id is None:
            duplicate += 1
            continue
        inserted += 1
    source_result.inserted = inserted
    source_result.duplicate = duplicate
    return source_result


def _filter_enabled(conn: DictConnection, source_classes: dict[str, type[NewsSource]]) -> dict[str, type[NewsSource]]:
    """Bug fix (P1-INGEST/ADMIN-API reconciliation audit): the Sources
    admin screen's own copy says "Collection runs every enabled method
    per source" -- until this function existed, that was false. Nothing
    upstream of this ever consulted `sources.enabled`
    (migrations/0002_reference_tables.sql); `run_collection` fetched
    from the full code registry regardless of what an admin had
    disabled via PATCH/toggle `/sources/{id}`.

    Excludes any `source_classes` entry whose registry key is marked
    `enabled = FALSE` in the DB `sources` table, correlated via the
    stable `sources.registry_key` column (migrations/
    0014_sources_registry_key.sql) -- never `sources.name`, which is
    admin-editable and would silently stop matching the instant someone
    renames a source (the companion bug this same migration fixes for
    test/diagnose, app.services.admin._plugin_for_registry_key).

    A registry key with NO corresponding `sources` row (or a `sources`
    row whose `registry_key` is still NULL -- migrations/0014's own
    docstring: 6 of 51 seeded rows have no ported plugin) is left
    untouched: silence from the DB is not a disable vote. This also
    keeps a `conn` double with an empty/unpopulated `sources` table
    (test doubles, a freshly migrated test schema) a no-op, by
    construction, without special-casing tests here.
    """
    disabled = {
        row["registry_key"]
        for row in conn.execute(
            "SELECT registry_key FROM sources WHERE registry_key IS NOT NULL AND enabled = FALSE"
        ).fetchall()
    }
    if not disabled:
        return source_classes
    return {key: cls for key, cls in source_classes.items() if key not in disabled}


def _start_run(conn: DictConnection, *, trigger: str) -> int:
    """Insert the `runs` row for this collection run and commit
    IMMEDIATELY — RT-1's whole point is that a crash one line later must
    still leave a durable `status='running'` row (the schema default)
    for a watchdog to find via `idx_runs_status_running` +
    `heartbeat_at`, which an uncommitted INSERT sitting inside a larger
    transaction cannot provide.
    """
    row = conn.execute("INSERT INTO runs (trigger) VALUES (%s) RETURNING id", (trigger,)).fetchone()
    if row is None:
        raise RuntimeError("INSERT INTO runs ... RETURNING id returned no row")
    run_id = int(row["id"])
    conn.commit()
    return run_id


def _touch_heartbeat(conn: DictConnection, run_id: int) -> None:
    """Committed immediately, same reasoning as `_start_run` — a
    heartbeat sitting in an uncommitted transaction is invisible to any
    external watchdog process trying to read it.
    """
    queries.touch_run_heartbeat(conn, run_id)
    conn.commit()


def _finalize_run(
    conn: DictConnection,
    run_id: int,
    *,
    status: str,
    total_new: int,
    total_matched: int,
    details: dict[str, Any],
) -> None:
    """Mark the run `'done'` or `'error'` with final counts and a
    per-source `details` blob (mirrors legacy-behavior.md §2.6's
    `runs.details` shape — one JSON object keyed by source name) — the
    data the Source-health screen (frontend-contract.md §3.10) and any
    run-history view need. Committed immediately, same reasoning as
    `_start_run`/`_touch_heartbeat`.
    """
    conn.execute(
        """
        UPDATE runs
        SET status = %s, finished_at = now(), heartbeat_at = now(),
            total_new = %s, total_matched = %s, details = %s
        WHERE id = %s
        """,
        (status, total_new, total_matched, Jsonb(details), run_id),
    )
    conn.commit()


def run_collection(
    conn: DictConnection | None,
    *,
    source_classes: dict[str, type[NewsSource]] | None = None,
    max_workers: int = SOURCE_PARALLELISM,
    trigger: str = "scheduled",
    progress_cb: Callable[[SourceResult], None] | None = None,
) -> CollectionRunResult:
    """The P1 entrypoint. `conn=None` runs fetch-only (discovery, no DB
    write, no run-tracking — there's no DB to track a run in) — used
    when `DATABASE_URL` isn't configured (local dev / CI) and by the
    live-measurement script, which times discovery specifically.

    When `conn` is given, this creates a `runs` row up front, touches
    its heartbeat once per completed source, and finalizes it
    (`status='done'`) on a clean finish or (`status='error'`) if
    anything survives per-source isolation and escapes this function —
    RT-1 (docs/red-team-findings.md): before this, no run left any
    trace of having happened, succeeded, or crashed.

    Per-source writes are committed as each source finishes (see below),
    not batched into one commit at the very end — `_touch_heartbeat`'s
    own `conn.commit()`, called once per completed source, flushes
    whatever `write_items` inserted for that source moments earlier on
    the same connection. What stays batched is WITHIN a source (all of
    one source's articles in a single `write_items` call), never one
    commit per article row (speed-techniques.md §4 point 6). Only the
    run-lifecycle rows are otherwise committed early/separately, for the
    same crash-survival reason `_start_run`/`_touch_heartbeat`/
    `_finalize_run` document.

    `progress_cb`, if given, is called once per source with that
    source's final `SourceResult`, in the same place/order `_record`
    below already processes it (i.e., real-time, via `fetch_all`'s
    `on_result` hook — see that function's docstring) — not after the
    whole run finishes. This is the hook ADMIN-API's manual "run
    collection now" trigger (`app.services.admin.start_fetch_run`) uses
    to publish genuine per-source progress ("23/45 done, and which
    ones") to a caller polling an HTTP status endpoint while a multi-
    minute run is still in flight, instead of the run being invisible
    until it completes. `None` (the default) preserves every existing
    caller's behavior exactly.

    When `conn` is given, the set of sources actually fetched is also
    filtered down to those the DB `sources` table marks enabled (see
    `_filter_enabled`) — applied whether `source_classes` was passed
    explicitly (e.g. `app.batch.run_daily.main()`, which pre-loads the
    full registry) or left `None` (defaulting to the full registry
    here). `conn=None` skips this — there's no `sources` table to
    consult without a DB, so fetch-only runs stay unfiltered.
    """
    start = time.monotonic()
    run_id: int | None = None
    if conn is not None:
        run_id = _start_run(conn, trigger=trigger)

    run_result = CollectionRunResult(run_id=run_id)
    details: dict[str, Any] = {}
    try:
        effective_source_classes = source_classes
        if conn is not None:
            if effective_source_classes is None:
                load_all()
                effective_source_classes = all_sources()
            effective_source_classes = _filter_enabled(conn, effective_source_classes)
        day = today_dhaka()

        def _record(source_result: SourceResult, items: list[FeedItem]) -> None:
            if conn is not None and items:
                source_result = write_items(conn, source_result, items, day=day)
            run_result.sources.append(source_result)
            run_result.total_fetched += source_result.fetched
            run_result.total_inserted += source_result.inserted
            run_result.total_duplicate += source_result.duplicate
            details[source_result.display_name] = {
                "status": source_result.status,
                "fetched": source_result.fetched,
                "inserted": source_result.inserted,
                "duplicate": source_result.duplicate,
                "duration_seconds": round(source_result.duration_seconds, 2),
                "error": source_result.error,
            }
            if conn is not None and run_id is not None:
                _touch_heartbeat(conn, run_id)
            if progress_cb is not None:
                progress_cb(source_result)

        fetch_all(
            effective_source_classes,
            max_workers=max_workers,
            on_result=lambda _key, source_result, items: _record(source_result, items),
        )
    except Exception:
        if conn is not None and run_id is not None:
            try:
                _finalize_run(
                    conn,
                    run_id,
                    status="error",
                    total_new=run_result.total_inserted,
                    total_matched=0,
                    details=details,
                )
            except Exception:
                logger.error("run_finalize_after_crash_failed", extra={"run_id": run_id})
        raise
    finally:
        run_result.wall_seconds = time.monotonic() - start

    if conn is not None and run_id is not None:
        # total_matched stays 0 HERE on purpose: P2 (keyword matching)
        # is a separate phase that has not run yet at this point, so the
        # collector genuinely does not know the number. It is filled in
        # afterwards by `app.services.admin.record_run_match_total()`.
        # Before that existed, nothing ever wrote it and the Source
        # health page's "MATCHED LAST RUN" tile read 0 on every run
        # forever — a KPI that was structurally incapable of being right.
        _finalize_run(
            conn, run_id, status="done", total_new=run_result.total_inserted, total_matched=0, details=details
        )
    return run_result

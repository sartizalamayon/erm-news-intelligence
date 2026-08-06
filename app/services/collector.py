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
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date

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
) -> dict[str, tuple[SourceResult, list[FeedItem]]]:
    """Tier 1 of the two-tier fan-out. `source_classes=None` loads and
    uses the full registry; pass an explicit dict (including `{}`) to
    run a subset — the mechanism `tests/test_p1_collector.py` and the
    live-measurement script both use to avoid hardcoding the registry.
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
            results[key] = future.result()
    return results


def write_items(conn: DictConnection, source_result: SourceResult, items: list[FeedItem], *, day: date) -> SourceResult:
    """Dedup-on-insert write path (legacy-behavior.md §2.6 minus P2's
    match step). Two-step dedup, per app/db/queries.py's own docstring:

    1. `seen_urls` first — catches a URL whose `articles` row already
       aged out of the retention window (storage-policy.md), which the
       live `articles.url_norm` index alone cannot catch.
    2. `insert_article()`'s own `ON CONFLICT (url_norm) DO NOTHING`
       against currently-live rows — a single indexed lookup baked into
       the INSERT itself, never the legacy `WHERE url=? OR url_norm=?`
       full-table scan (ADR §2, the single worst bottleneck in the old
       system — explicitly not reintroduced here).
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
        if url_norm:
            queries.remember_seen_url(conn, url_norm, day)
    source_result.inserted = inserted
    source_result.duplicate = duplicate
    return source_result


def run_collection(
    conn: DictConnection | None,
    *,
    source_classes: dict[str, type[NewsSource]] | None = None,
    max_workers: int = SOURCE_PARALLELISM,
) -> CollectionRunResult:
    """The P1 entrypoint. `conn=None` runs fetch-only (discovery, no DB
    write) — used when `DATABASE_URL` isn't configured (local dev / CI)
    and by the live-measurement script, which times discovery
    specifically. All writes for the whole run share ONE transaction
    (the caller's `conn`, committed once by `app.db.connection.get_connection`'s
    `with` block) — batched, not per-row commits (speed-techniques.md §4
    point 6; §3#13 explicitly rejects per-row transactions as a
    Postgres-MVCC-specific pattern this SQLite/Postgres-agnostic design
    doesn't need).
    """
    start = time.monotonic()
    fetched = fetch_all(source_classes, max_workers=max_workers)
    day = today_dhaka()

    run_result = CollectionRunResult()
    for source_result, items in fetched.values():
        if conn is not None and items:
            source_result = write_items(conn, source_result, items, day=day)
        run_result.sources.append(source_result)
        run_result.total_fetched += source_result.fetched
        run_result.total_inserted += source_result.inserted
        run_result.total_duplicate += source_result.duplicate
    run_result.wall_seconds = time.monotonic() - start
    return run_result

"""Daily batch orchestration.

Today this is a no-op that proves the entrypoint shape end to end
(config loads, logging configures, exit code 0) so CI and
`python -m app.batch` both succeed before any real logic lands. It is
deliberately NOT a placeholder function that just `pass`es — it does
real (if trivial) work so a smoke test can assert something meaningful
about it.

TODO(P1-FETCH): replace the no-op body with the real run:
  1. `app.sources.registry.all_sources()` for the configured outlets.
  2. Two-tier bounded fan-out across them (speed-techniques.md §4
     points 2-3): sources concurrently, articles within a source
     concurrently, both bounded, plus a process-wide semaphore.
  3. Wrap each source's run in try/except on app.errors.SourceError
     (never a bare `except Exception`) so one failing source cannot
     abort the run — record it and continue.
  4. Single-method feed discovery with fallback only on empty result
     (speed-techniques.md §4 point 4); log whenever the fallback fires.

TODO(ARCH-DB): own the write path once app/db exists — indexed
upsert dedup (speed-techniques.md §4 point 5: unique constraint +
`ON CONFLICT DO NOTHING`) and batched transactions, not per-row commits
(§4 point 6 — SQLite/Postgres both prefer batching here; per-row
transactions are explicitly rejected as a Postgres-MVCC-specific
pattern that doesn't help this design).
"""

from __future__ import annotations

from app.config import get_settings, validate_production_settings
from app.logging import configure_logging, get_logger
from app.sources.registry import all_sources

logger = get_logger(__name__)


def main() -> int:
    configure_logging()
    settings = get_settings()
    validate_production_settings(settings)

    sources = all_sources()
    logger.info(
        "batch_run_start",
        extra={"app_env": settings.app_env, "registered_sources": len(sources)},
    )

    # TODO(P1-FETCH): replace with the real per-source fan-out described
    # above. Iterating an empty registry and exiting 0 is the correct
    # behavior today, not a shortcut — there is nothing to run yet.
    for source_key in sources:
        logger.info("source_run_skipped_not_implemented", extra={"source": source_key})

    logger.info("batch_run_complete", extra={"sources_run": 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

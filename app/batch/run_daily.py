"""Daily batch orchestration — the whole unattended pipeline.

`main()` loads the full source registry (app/sources/registry.py),
runs the two-tier bounded fan-out (app/services/collector.py), and
writes results in one batched transaction when `DATABASE_URL` is
configured. When it isn't (local dev / CI default — see
tests/conftest.py's env defaults), this runs fetch-only: real discovery,
no DB write, so the entrypoint stays exercisable without a live
Postgres. `validate_production_settings` still fails fast if
`app_env=production` and `DATABASE_URL` is genuinely missing.

Phases, in order, and why that order:

    reap stale runs -> collect -> ENRICH (P3 LLM) -> rollups -> market

The enrichment phase was missing entirely until recently, and its
absence is worth naming here because everything downstream depends on
it: `app.services.enrichment.run_llm_enrichment` had exactly one caller
in the whole repo, the admin console's per-stage button, so the cron
produced articles, rollups and prices every morning with nothing scored,
translated, deduped or clustered unless somebody opened the Run screen
and clicked through the stages by hand. See `run_enrichment` below for
the ordering constraint against the rollups, the wall-clock budget, and
what does and does not count as a failed run.

Per-source isolation is enforced one level down, inside
`app.services.collector.fetch_all`/`_run_one_source` — a single
source's `SourceError` (or, deliberately, any unexpected exception —
see that module's docstring) never aborts this entrypoint; it is
recorded as that source's own `failed` status and logged.

`run_collection(conn, ..., trigger="scheduled")` — this is the only
real entrypoint, wired to the daily GitHub Actions cron (ADR §1), so
every run through here is `'scheduled'`, not `'manual'` (the schema's
own default, meant for a future admin-triggered "run now" button this
module doesn't implement). Run-lifecycle bookkeeping itself
(`runs` row created/heartbeated/finalized) lives in
`app.services.collector.run_collection` — see that module's docstring
for why (RT-1, docs/red-team-findings.md).

**RT-1-residual (docs/red-team-findings.md; DEPLOY mission):** before
`run_collection` touches the `runs` table again, this entrypoint first
calls `app.db.reaper.reap_stale_runs(conn)` — marking any *previous*
run still stuck at `status='running'` with a heartbeat that stopped
advancing (a hard kill: SIGKILL, OOM, or `daily-batch.yml`'s own
`timeout-minutes`, none of which `run_collection`'s own exception
handling can observe) as `status='error'` before starting fresh. Every
new invocation of this entrypoint — scheduled or manual — self-heals any
abandoned row from a previous one, by construction. See
`app/db/reaper.py`'s module docstring for the full RT-1-residual
writeup and the stale-threshold reasoning.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import date

from app.config import get_settings, validate_production_settings
from app.db.connection import DictConnection, get_connection
from app.db.reaper import reap_stale_runs
from app.db.rollups import compute_rollups_for_day
from app.logging import configure_logging, get_logger
from app.services import market as market_service
from app.services.collector import run_collection
from app.sources._util import today_dhaka
from app.sources.registry import all_sources, load_all

logger = get_logger(__name__)

# How long the LLM enrichment phase may run before it stops asking for
# more work, in minutes, overridable with BATCH_ENRICHMENT_BUDGET_MINUTES
# (same plain-environment convention `app.services.enrichment` already
# uses for LLM_TRANSLATE/LLM_DEDUP/LLM_JUDGE/LLM_TOPIC/LLM_BATCH, rather
# than a Settings field this module would have to own).
#
# The arithmetic, against `.github/workflows/daily-batch.yml`'s
# `timeout-minutes: 150`: checkout, pip install and `python -m app.db.migrate`
# are a few minutes; collection is ~2.5 minutes measured (a real
# 45-source run took 2m17s); rollups and the market refresh are seconds
# to a couple of minutes. token-budget.md §4's central estimate for a
# full enrichment day is ~73 minutes, rate-limit-paced rather than
# compute-bound. 110 leaves that estimate ~50% of headroom and still
# lands the whole job ~30 minutes clear of the workflow's own ceiling.
#
# This budget exists because of what a SIGKILL costs, not because of
# what a long run costs. Actions kills the process at the timeout, and
# Python's `finally` blocks are not reliably reached, so a run that
# overshoots loses the rollups and the market refresh that come after
# it -- work that is cheap, and that the whole long-term dashboard reads
# from. Stopping enrichment early loses nothing permanently: every stage
# commits as it goes, and unfinished rows keep `llm_score IS NULL`, which
# is exactly the selection the next run picks up.
ENRICHMENT_BUDGET_MINUTES_DEFAULT = 110.0


def _enrichment_budget_minutes() -> float:
    raw = os.environ.get("BATCH_ENRICHMENT_BUDGET_MINUTES", "")
    if not raw.strip():
        return ENRICHMENT_BUDGET_MINUTES_DEFAULT
    try:
        minutes = float(raw)
    except ValueError:
        logger.warning("enrichment_budget_unparseable", extra={"value": raw})
        return ENRICHMENT_BUDGET_MINUTES_DEFAULT
    if minutes <= 0:
        logger.warning("enrichment_budget_not_positive", extra={"value": raw})
        return ENRICHMENT_BUDGET_MINUTES_DEFAULT
    return minutes


def _deadline_stopper(budget_minutes: float) -> Callable[[], bool]:
    """A `should_stop` callback that goes True once the budget is spent.

    `app.services.enrichment.run_llm_enrichment` polls this between
    stages, between translate persistence chunks and between judge
    batches — every point at which the previous work is already
    committed — so hitting the deadline ends the phase on a durable
    boundary rather than mid-write.
    """
    deadline = time.monotonic() + budget_minutes * 60.0

    def _stop() -> bool:
        return time.monotonic() >= deadline

    return _stop


def run_enrichment(conn: DictConnection, *, day: date) -> bool:
    """The P3 LLM phase of the daily batch. Returns True if it can
    honestly be called a success.

    WHY THIS IS HERE AT ALL
    ───────────────────────
    It was not, and that was the defect. This module imported the
    collector, the reaper, the rollups and the market refresh, and never
    `app.services.enrichment`. Its only caller anywhere was the
    admin console's per-stage button
    (`app.services.admin.run_pipeline_stage`), so the cron wrote
    articles, rollups and prices every morning while nothing was ever
    translated, deduped, scored or clustered unless a human opened the
    Run screen and clicked through five stages by hand. Everything
    downstream — the relevance tiers, the review queue's ordering, the
    score bands the CRO boards are built from, the brief — waited on
    that click. `.github/workflows/daily-batch.yml`'s own header already
    described this as the intended shape ("once P3-LLM wires it in, the
    LLM enrichment stages too") and its 150-minute timeout was sized for
    it.

    `stages=None` on purpose: that is `run_llm_enrichment`'s documented
    FULL RUN, the one whose row selection and per-stage gating
    (LLM_TRANSLATE / LLM_DEDUP / LLM_JUDGE / LLM_TOPIC) mirror legacy's
    daily pass. Calling the five admin stage keys one at a time would be
    a different thing: each re-selects rows under its own precedence
    branch (`enrichment._select_where`), and "bodies" is a network
    page-fetch this batch has no business doing per-article.

    WHAT COUNTS AS FAILURE
    ──────────────────────
    Not "zero rows". A quiet day with nothing pending is a success, and
    so is a day where no provider is configured for a feature — that is
    a documented degradation (`skipped_stages`), logged here, and the
    stage genuinely did not attempt anything.

    A stage that DID attempt LLM work and got nothing back is a failure,
    always, even though every stage substitutes a neutral default and
    returns normally. That is the exact bug `enrichment.StageCalls`
    exists to make visible: a provider once ran with a base_url that
    404'd, 578 articles kept their Bengali headlines, and the run
    reported success. Reported here as a non-zero exit, which is the
    only failure signal the workflow can see, and which opens the
    tracking issue DoD #4 already wires up.

    Note the judge stage in particular: `app.services.llm.parsing` no
    longer invents a score of 5 for an unreadable judge response — it
    raises, and `run_llm_enrichment` catches that per batch, leaves
    those rows with `llm_score IS NULL` (still pending, still selected
    by the next run) and records the failure in `StageCalls`. So a
    provider returning garbage all day leaves rows honestly unscored
    AND fails this function, rather than either dying mid-batch or
    quietly filling the corpus with fabricated 5s.
    """
    from app.services import enrichment  # lazy: keeps the batch importable if P3 is mid-edit

    budget = _enrichment_budget_minutes()
    should_stop = _deadline_stopper(budget)
    started = time.monotonic()
    logger.info("enrichment_start", extra={"day": day.isoformat(), "budget_minutes": budget})

    try:
        result = enrichment.run_llm_enrichment(conn, today=day, should_stop=should_stop)
    except Exception:
        # The day's collection is already committed and is the
        # irreplaceable part of this job; the rollups and the market
        # refresh below are still worth running. Fail the job, but only
        # after the cheap remaining work has been done.
        conn.rollback()
        logger.error("enrichment_failed", exc_info=True)
        return False

    dead = result.wholly_failed_stages()
    logger.info(
        "enrichment_complete",
        extra={
            "articles_selected": result.enriched,
            "skipped_stages": list(result.skipped_stages),
            "failed_calls": result.total_failed_calls,
            "wholly_failed_stages": list(dead),
            "elapsed_seconds": round(time.monotonic() - started, 1),
        },
    )
    if result.skipped_stages:
        logger.warning("enrichment_stages_had_no_provider", extra={"stages": list(result.skipped_stages)})
    if should_stop():
        # Not a failure: the work that did land is committed, and what
        # did not still has llm_score IS NULL, so tomorrow's run selects
        # it again. Loud because a budget hit two days running means the
        # backlog is growing faster than the budget can clear it.
        logger.warning("enrichment_stopped_at_budget", extra={"budget_minutes": budget})
    if dead:
        logger.error(
            "enrichment_stages_attempted_and_got_nothing_back",
            extra={
                "stages": list(dead),
                "errors": {name: result.calls[name].first_error for name in dead},
            },
        )
        return False
    return True


def main() -> int:
    configure_logging()
    settings = get_settings()
    validate_production_settings(settings)

    load_all()
    sources = all_sources()
    logger.info(
        "batch_run_start",
        extra={"app_env": settings.app_env, "registered_sources": len(sources)},
    )

    if not sources:
        logger.info("batch_run_complete", extra={"sources_run": 0})
        return 0

    if settings.database_url:
        with get_connection() as conn:
            reaped = reap_stale_runs(conn)
            if reaped:
                logger.warning(
                    "stale_runs_reaped_before_run",
                    extra={"run_ids": reaped, "count": len(reaped)},
                )
            result = run_collection(conn, source_classes=sources)

            # P3, on the same day the collection filed under. BEFORE the
            # rollups on purpose: `rollup_monthly_coverage.scored_count`
            # and `rollup_monthly_score_band` are folded from
            # `articles.llm_score`, so rolling up first would publish a
            # month whose scored counts are a day behind its article
            # counts, every day, and the long-term boards read those
            # rollups rather than the raw rows. See `run_enrichment` for
            # what it does and what counts as a failure.
            enrichment_ok = run_enrichment(conn, day=today_dhaka())

            # DASHBOARD-ROLLUPS: fold today's collection into the current
            # month's rollup_monthly_* rows (migrations/0007_rollups.sql)
            # so the long-term dashboard tabs have data — once per real
            # batch run, not a separate cron. Idempotent
            # (app.db.rollups module docstring): re-running this same
            # batch for the same day recomputes the month from scratch
            # and overwrites, never double-counts.
            rollup_counts = compute_rollups_for_day(conn, today_dhaka())
            conn.commit()
            logger.info("rollups_updated", extra=rollup_counts)

            # Exchange prices for the Long-term dashboard's Market tab.
            # Deliberately AFTER the rollups and inside its own try: the
            # exchange is a third party that can be down or slow, and a
            # dead scrape must not cost us the day's collection, which is
            # the irreplaceable part of this job. `refresh` already
            # swallows its own network errors and returns counts; this
            # guard only covers a DB failure while storing.
            try:
                market_counts = market_service.refresh(conn)
                conn.commit()
                logger.info("market_refreshed", extra=market_counts)
            except Exception:
                conn.rollback()
                logger.warning("market_refresh_failed", exc_info=True)
    else:
        logger.warning(
            "batch_run_no_database_configured",
            extra={"app_env": settings.app_env, "note": "running fetch-only, nothing will be written"},
        )
        result = run_collection(None, source_classes=sources)
        # No DB means no rows to enrich and nowhere to write a score, so
        # there is nothing here to succeed or fail at. Stays True so a
        # fetch-only dev/CI invocation keeps exiting 0.
        enrichment_ok = True

    logger.info(
        "batch_run_complete",
        extra={
            "sources_run": len(result.sources),
            "sources_ok": sum(1 for s in result.sources if s.status == "ok"),
            "sources_empty": result.total_empty_sources,
            "sources_failed": result.total_failed_sources,
            "total_fetched": result.total_fetched,
            "total_inserted": result.total_inserted,
            "total_duplicate": result.total_duplicate,
            "enrichment_ok": enrichment_ok,
        },
    )
    # Non-zero is the only failure signal `.github/workflows/daily-batch.yml`
    # can act on, and acting on it is the point: it opens/updates the
    # tracking issue (DoD #4). Deliberately returned AFTER the rollups
    # and the market refresh have run — a broken LLM provider must not
    # also cost the day the cheap work that does not depend on it.
    return 0 if enrichment_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

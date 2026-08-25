"""The daily cron has to run the LLM stages, not just collect.

THE DEFECT THESE TESTS PIN
──────────────────────────
`app/batch/run_daily.py` imported the collector, the reaper, the rollups
and the market refresh, and never `app.services.enrichment`.
`run_llm_enrichment` had exactly one caller in the entire repo: the
admin console's per-stage button (`app.services.admin.run_pipeline_stage`).

So the scheduled run wrote articles, rollups and exchange prices every
morning, and nothing was ever translated, deduplicated, scored or topic-
clustered unless a human opened the Run screen and clicked through the
stages by hand. The relevance tiers, the review queue's ordering, the
score bands the CRO boards read, and the brief all sit downstream of
that click. `.github/workflows/daily-batch.yml` had already been written
in anticipation of the wiring ("once P3-LLM wires it in, the LLM
enrichment stages too") and its 150-minute timeout sized for it.

WHAT IS ASSERTED HERE, BEYOND "IT IS CALLED"
────────────────────────────────────────────
The ordering against the rollups (they read `llm_score`), the wall-clock
budget that keeps the phase inside the workflow's timeout, and — the
part worth the most — that a stage which attempted LLM work and got
nothing back FAILS the run instead of reporting a clean day over
silently unenriched articles.

No LLM tokens are spent anywhere in this file.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.batch.run_daily import _deadline_stopper, _enrichment_budget_minutes
from app.batch.run_daily import main as batch_main
from app.db.testing import migrated_schema, pg_available
from app.services.enrichment import EnrichmentResult, StageCalls
from app.services.llm.provider import LLMClient
from app.sources._util import today_dhaka
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from tests.helpers.fake_llm import FakeLLMProvider

pg_gated = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


class _OneArticleSource(NewsSource):
    key = "batch_enrich_one"
    display_name = "Batch Enrich One"

    def iter_feed_items(self) -> list[FeedItem]:
        return [FeedItem(url="https://enrich.example/a", title="A headline", source_key=self.key)]

    def fetch_article(self, item: FeedItem) -> FetchedArticle:  # pragma: no cover - never called
        return FetchedArticle(item=item, body_text="")


def _wire_batch(monkeypatch: pytest.MonkeyPatch, conn) -> None:
    """Run the REAL `app.batch.run_daily.main()` against a test schema.

    Same monkeypatch harness tests/test_reaper_stale_runs.py and
    tests/test_redteam_orphaned_runs.py use: swap the connection factory
    and the source registry, leave every other phase of the batch
    genuinely running.
    """
    import app.batch.run_daily as run_daily_module
    import app.db.connection as connection_module

    @contextmanager
    def _fake_get_connection(dsn: str | None = None):
        yield conn

    monkeypatch.setattr(connection_module, "get_connection", _fake_get_connection)
    monkeypatch.setattr(run_daily_module, "get_connection", _fake_get_connection)
    monkeypatch.setattr(run_daily_module, "all_sources", lambda: {_OneArticleSource.key: _OneArticleSource})
    monkeypatch.setattr(run_daily_module, "load_all", lambda: None)
    monkeypatch.setattr(
        run_daily_module,
        "get_settings",
        lambda: type("S", (), {"app_env": "test", "database_url": "postgresql:///fake"})(),
    )
    monkeypatch.setattr(run_daily_module, "validate_production_settings", lambda settings: None)


def _stub_enrichment(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    import app.services.enrichment as enrichment_module

    monkeypatch.setattr(enrichment_module, "run_llm_enrichment", fn)


# ── it runs at all, on the right day, in the right place ────────────────


@pg_gated
def test_the_scheduled_batch_runs_llm_enrichment(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def _record(conn, **kwargs):
        calls.append(kwargs)
        return EnrichmentResult(enriched=0)

    with migrated_schema() as conn:
        _wire_batch(monkeypatch, conn)
        _stub_enrichment(monkeypatch, _record)

        assert batch_main() == 0

    assert len(calls) == 1, "the daily batch must run the enrichment phase exactly once"
    assert calls[0]["today"] == today_dhaka(), "enrichment must cover the day collection just filed under"
    assert callable(calls[0]["should_stop"]), "the phase must be bounded, see run_enrichment's budget"


@pg_gated
def test_enrichment_runs_before_the_rollups_that_read_its_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rollup_monthly_coverage.scored_count` and
    `rollup_monthly_score_band` are folded from `articles.llm_score`.
    Rolling up first would publish a month whose scored counts are a day
    behind its article counts, every day — and the long-term boards read
    the rollups, not the raw rows.
    """
    import app.batch.run_daily as run_daily_module

    order: list[str] = []

    def _enrich(conn, **kwargs):
        order.append("enrich")
        return EnrichmentResult(enriched=0)

    real_rollups = run_daily_module.compute_rollups_for_day

    def _rollups(conn, day):
        order.append("rollups")
        return real_rollups(conn, day)

    with migrated_schema() as conn:
        _wire_batch(monkeypatch, conn)
        _stub_enrichment(monkeypatch, _enrich)
        monkeypatch.setattr(run_daily_module, "compute_rollups_for_day", _rollups)

        assert batch_main() == 0

    assert order == ["enrich", "rollups"]


# ── what counts as a failed run, and what does not ──────────────────────


@pg_gated
def test_a_stage_that_attempted_llm_work_and_got_nothing_back_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure mode `StageCalls` exists to expose: every stage
    substitutes a neutral default on a failed call and returns normally,
    so a provider whose every call 404s produced a run that looked
    perfect. Non-zero is the only signal the workflow can act on — it is
    what opens the tracking issue.
    """
    def _all_calls_failed(conn, **kwargs):
        return EnrichmentResult(
            enriched=12,
            calls={"judge": StageCalls(attempted=4, succeeded=0, failed=4, first_error="404 from the provider")},
        )

    with migrated_schema() as conn:
        _wire_batch(monkeypatch, conn)
        _stub_enrichment(monkeypatch, _all_calls_failed)

        assert batch_main() == 1

        # And the cheap work that does not depend on the LLM still ran:
        # a broken provider must not also cost the day its rollups.
        rolled = conn.execute("SELECT count(*) AS n FROM rollup_monthly_coverage").fetchone()
        assert rolled["n"] >= 1


@pg_gated
def test_a_feature_with_no_provider_configured_is_not_a_failed_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A documented degradation, not a fault: nothing was attempted, so
    there is nothing to report as failed. Treating this as a failure
    would page somebody every morning of a deployment that has not
    finished configuring its providers.
    """
    def _nothing_configured(conn, **kwargs):
        return EnrichmentResult(enriched=3, skipped_stages=("translate", "dedup", "judge", "topic"))

    with migrated_schema() as conn:
        _wire_batch(monkeypatch, conn)
        _stub_enrichment(monkeypatch, _nothing_configured)

        assert batch_main() == 0


@pg_gated
def test_an_exception_inside_enrichment_still_leaves_the_collection_and_rollups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The day's articles are the irreplaceable part of this job. An
    enrichment crash fails the run, but only after everything cheap that
    does not depend on it has been done.
    """
    def _boom(conn, **kwargs):
        raise RuntimeError("provider exploded")

    with migrated_schema() as conn:
        _wire_batch(monkeypatch, conn)
        _stub_enrichment(monkeypatch, _boom)

        assert batch_main() == 1

        assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM rollup_monthly_coverage").fetchone()["n"] >= 1
        newest = conn.execute("SELECT status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert newest["status"] == "done", "the collection run itself finished cleanly and must say so"


# ── the real enrichment path, no stub ───────────────────────────────────


@pg_gated
def test_a_judge_that_cannot_be_read_leaves_rows_unscored_and_fails_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`app.services.llm.parsing` no longer invents a score of 5 for an
    unreadable judge response — it raises. Through the batch that must
    mean: rows keep `llm_score IS NULL` (still pending, still selected
    by tomorrow's run), the process does not die, and the run reports
    failure rather than a clean day.

    The whole real `run_llm_enrichment` runs here; only the transport is
    fake.
    """
    import app.services.enrichment as enrichment_module

    def _unreadable(prompt: str, system: str) -> str:
        return "I'm afraid I can't help with that."

    def _fake_try_build(conn, feature):
        if feature == "scoring":
            return LLMClient(transport=FakeLLMProvider(responses=_unreadable), name="fake-scoring")
        return None

    monkeypatch.setattr(enrichment_module, "_try_build_client", _fake_try_build)

    with migrated_schema() as conn:
        _wire_batch(monkeypatch, conn)

        assert batch_main() == 1

        rows = conn.execute("SELECT llm_score, review_status FROM articles").fetchall()
        assert len(rows) == 1
        assert rows[0]["llm_score"] is None, "an unreadable judgment must leave the row unscored, never guess"
        assert rows[0]["review_status"] == "pending"


@pg_gated
def test_a_working_judge_scores_the_days_articles_through_the_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive case, end to end: collect, then score, with nobody
    clicking anything.
    """
    import app.services.enrichment as enrichment_module
    from tests.helpers.fake_llm import judge_batch_response

    def _fake_try_build(conn, feature):
        if feature == "scoring":
            return LLMClient(
                transport=FakeLLMProvider(responses=lambda p, s: judge_batch_response([(1, 9, "very relevant")])),
                name="fake-scoring",
            )
        return None

    monkeypatch.setattr(enrichment_module, "_try_build_client", _fake_try_build)

    with migrated_schema() as conn:
        _wire_batch(monkeypatch, conn)

        assert batch_main() == 0

        row = conn.execute("SELECT llm_score, llm_reason FROM articles").fetchone()
        assert row["llm_score"] == 9.0
        assert row["llm_reason"] == "very relevant"


# ── the budget ──────────────────────────────────────────────────────────


def test_the_budget_is_a_deadline_the_enrichment_phase_polls() -> None:
    assert _deadline_stopper(10.0)() is False
    assert _deadline_stopper(0.0)() is True  # already spent


def test_the_budget_falls_back_to_the_documented_default_on_junk(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.batch.run_daily import ENRICHMENT_BUDGET_MINUTES_DEFAULT

    monkeypatch.delenv("BATCH_ENRICHMENT_BUDGET_MINUTES", raising=False)
    assert _enrichment_budget_minutes() == ENRICHMENT_BUDGET_MINUTES_DEFAULT

    monkeypatch.setenv("BATCH_ENRICHMENT_BUDGET_MINUTES", "45")
    assert _enrichment_budget_minutes() == 45.0

    for junk in ("", "   ", "soon", "0", "-5"):
        monkeypatch.setenv("BATCH_ENRICHMENT_BUDGET_MINUTES", junk)
        assert _enrichment_budget_minutes() == ENRICHMENT_BUDGET_MINUTES_DEFAULT, (
            f"{junk!r} must not silently become the budget — an unbounded or instantly-spent "
            "phase is how a run overshoots the workflow's own timeout and loses the rollups"
        )


@pg_gated
def test_an_exhausted_budget_stops_the_phase_without_failing_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopping early loses nothing permanently — every stage commits as
    it goes and unfinished rows keep `llm_score IS NULL` — so it is a
    loud warning, not a failure.
    """
    seen: list[bool] = []

    def _check_budget(conn, **kwargs):
        seen.append(kwargs["should_stop"]())
        return EnrichmentResult(enriched=0)

    monkeypatch.setenv("BATCH_ENRICHMENT_BUDGET_MINUTES", "0.0000001")
    with migrated_schema() as conn:
        _wire_batch(monkeypatch, conn)
        _stub_enrichment(monkeypatch, _check_budget)

        assert batch_main() == 0

    assert seen == [True]

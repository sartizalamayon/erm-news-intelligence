"""Source health overview/table/run-history + pipeline stage runs
(app.services.admin, app.api.routers.health).

Pipeline stage dispatch for translate/dedup/judge/topic goes through
`app.services.enrichment.run_llm_enrichment` (P3-LLM's module, imported
lazily by app.services.admin.run_pipeline_stage specifically so THIS
package's own concurrent churn can never break admin CRUD routes that
have nothing to do with the LLM layer -- see
app.services.admin._build_client_for_feature's docstring for the same
reasoning applied to suggest_refinement). Tests that need
app.services.enrichment to actually import call `_require_enrichment()`
first and SKIP (not fail) if it currently can't -- P3-LLM's package is
being concurrently refactored (a generic OpenAI-compatible provider
adapter) and may be mid-edit/transiently inconsistent at any given
moment; that is not this task's regression to carry as a red test.

The "bodies" stage (no existing orchestrator anywhere in this codebase
-- see app.services.admin's PIPELINE_STAGES docstring) has no such
dependency and is tested unconditionally, against a fake NewsSource
plugin, same pattern as tests/test_admin_sources.py.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import threading
import time

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.api.deps import AuthenticatedUser, get_current_user
from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.errors import ValidationError
from app.main import create_app
from app.services import admin as admin_service
from app.sources.base import FeedItem, FetchedArticle, NewsSource

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(id=9, email="admin@grameenphone.com", role="admin", display_name="Admin", session_id=9)


@dataclasses.dataclass
class _StubEnrichmentResult:
    """Duck-typed stand-in for app.services.enrichment.EnrichmentResult
    (fields: enriched, skipped_stages) -- admin.run_pipeline_stage only
    ever reads those two attributes, so tests that fully mock
    run_llm_enrichment don't need the real dataclass import.
    """

    enriched: int
    skipped_stages: tuple = ()


def _require_enrichment():
    """Import app.services.enrichment, skipping the calling test (not
    failing it) if that currently raises -- see module docstring.
    """
    try:
        from app.services import enrichment

        return enrichment
    except ImportError as exc:
        pytest.skip(f"app.services.enrichment is not importable right now (P3-LLM package mid-refactor): {exc}")


def _insert_run(
    conn, *, details: dict, status: str = "done", trigger: str = "scheduled", total_matched: int = 0
) -> int:
    row = conn.execute(
        "INSERT INTO runs (status, trigger, total_new, total_matched, details, finished_at) "
        "VALUES (%s, %s, 0, %s, %s, now()) RETURNING id",
        (status, trigger, total_matched, Jsonb(details)),
    ).fetchone()
    return row["id"]


# ── health_sources / health_overview / health_runs ───────────────────────


def test_health_sources_maps_run_details_to_status_and_note() -> None:
    with migrated_schema() as conn:
        admin_service.create_source(conn, name="Healthy Outlet", methods=["sitemap"], enabled=True)
        admin_service.create_source(conn, name="Quiet Outlet", methods=["rss"], enabled=True)
        admin_service.create_source(conn, name="Broken Outlet", methods=["api"], enabled=True)
        admin_service.create_source(conn, name="Off Outlet", methods=["rss"], enabled=False)
        admin_service.create_source(conn, name="Zero Fetch Outlet", methods=["id_enum"], enabled=True)
        _insert_run(
            conn,
            details={
                "Healthy Outlet": {"status": "ok", "fetched": 40, "inserted": 12, "error": None},
                "Quiet Outlet": {"status": "ok", "fetched": 10, "inserted": 0, "error": None},
                "Broken Outlet": {"status": "failed", "fetched": 0, "inserted": 0, "error": "timeout"},
                "Zero Fetch Outlet": {"status": "ok", "fetched": 0, "inserted": 0, "error": None},
            },
        )
        conn.commit()

        rows = {r["name"]: r for r in admin_service.health_sources(conn)}
        assert rows["Healthy Outlet"]["status"] == "healthy"
        assert rows["Quiet Outlet"]["status"] == "quiet"
        assert "already stored" in rows["Quiet Outlet"]["note"]
        assert rows["Broken Outlet"]["status"] == "error"
        assert "timeout" in rows["Broken Outlet"]["note"]
        assert rows["Off Outlet"]["status"] == "disabled"
        # status="ok" with fetched=0 (real observed case: Deshpatrika, Kalbela)
        # must NOT be mislabeled "healthy" with an empty note -- that's
        # visually identical to a fully successful run and defeats the
        # screen's purpose of distinguishing a quiet day from a broken parser.
        assert rows["Zero Fetch Outlet"]["status"] != "healthy"
        assert rows["Zero Fetch Outlet"]["status"] == "quiet"
        assert rows["Zero Fetch Outlet"]["note"]


def test_health_overview_computes_real_kpis_not_hardcoded() -> None:
    with migrated_schema() as conn:
        admin_service.create_source(conn, name="A", methods=["rss"], enabled=True)
        admin_service.create_source(conn, name="B", methods=["rss"], enabled=True)
        _insert_run(
            conn,
            details={
                "A": {"status": "ok", "fetched": 20, "inserted": 5, "error": None},
                "B": {"status": "failed", "fetched": 0, "inserted": 0, "error": "boom"},
            },
            total_matched=3,
        )
        conn.commit()

        overview = admin_service.health_overview(conn)
        assert overview["healthy"] == 1
        assert overview["needs_attention"] == 1
        assert overview["fetched_last_run"] == 20
        assert overview["matched_last_run"] == 3
        assert overview["last_run_at"] is not None
        assert len(overview["stages"]) == 5
        assert {s["key"] for s in overview["stages"]} == {"translate", "bodies", "dedup", "judge", "topic"}
        assert all(s["status"] == "not_run" for s in overview["stages"])


def test_health_runs_orders_most_recent_first() -> None:
    with migrated_schema() as conn:
        first = _insert_run(conn, details={}, trigger="scheduled")
        conn.commit()
        second = _insert_run(conn, details={}, trigger="manual")
        conn.commit()

        rows = admin_service.health_runs(conn, limit=5)
        assert [r["id"] for r in rows] == [second, first]
        assert rows[0]["trigger"] == "manual"


def test_health_runs_surfaces_heartbeat_and_flags_stale_running_row() -> None:
    """RT-1-residual (tests/test_redteam_orphaned_runs.py): a run whose
    heartbeat stopped advancing (simulating a hard SIGKILL between
    app.services.collector._start_run/_touch_heartbeat and
    _finalize_run, which app.db.reaper.reap_stale_runs only cleans up on
    the NEXT batch invocation) must be observable as stale through this
    read path in the meantime, not silently reported as still running.
    """
    with migrated_schema() as conn:
        stale_id = _insert_run(conn, details={}, status="running")
        conn.execute(
            "UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = %s", (stale_id,)
        )
        healthy_id = _insert_run(conn, details={}, status="running")
        conn.execute("UPDATE runs SET heartbeat_at = now() WHERE id = %s", (healthy_id,))
        conn.commit()

        rows = {r["id"]: r for r in admin_service.health_runs(conn, limit=5)}

        assert rows[stale_id]["heartbeat_at"] is not None
        assert rows[stale_id]["status"] == "running"
        assert rows[stale_id]["heartbeat_stale"] is True

        assert rows[healthy_id]["heartbeat_stale"] is False


def test_health_runs_finished_row_never_flagged_stale_regardless_of_heartbeat_age() -> None:
    with migrated_schema() as conn:
        done_id = _insert_run(conn, details={}, status="done")
        conn.execute("UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = %s", (done_id,))
        conn.commit()

        rows = {r["id"]: r for r in admin_service.health_runs(conn, limit=5)}
        assert rows[done_id]["heartbeat_stale"] is False


def test_router_health_runs_response_includes_heartbeat_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        import app.api.routers.health as health_router_module

        stale_id = _insert_run(conn, details={}, status="running")
        conn.execute(
            "UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = %s", (stale_id,)
        )
        conn.commit()

        monkeypatch.setattr(health_router_module, "get_connection", lambda: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            resp = client.get("/health/runs")
            assert resp.status_code == 200
            row = next(r for r in resp.json() if r["id"] == stale_id)
            assert row["heartbeat_stale"] is True
            assert row["heartbeat_at"] is not None


# ── Pipeline stage runs ──────────────────────────────────────────────────


def test_run_pipeline_stage_unknown_key_raises_validation_error() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.run_pipeline_stage(conn, "not_a_real_stage")


def test_run_pipeline_stage_translate_real_call_with_no_provider_configured_is_a_safe_noop() -> None:
    """No mocking: exercises the REAL app.services.enrichment.run_llm_enrichment.
    Safe because no provider is configured in the test environment (see
    module docstring) -- ConfigurationError is caught internally and the
    stage is recorded as skipped, never a real network call.
    """
    _require_enrichment()
    with migrated_schema() as conn:
        insert_article(
            conn,
            url="https://x.test/1",
            url_norm="https://x.test/1",
            source="X",
            title="A Bengali headline",
            day=dt.date.today(),
        )
        conn.commit()

        result = admin_service.run_pipeline_stage(conn, "translate")
        assert result["stage"] == "translate"
        assert result["status"] == "done"

        statuses = {s["key"]: s for s in admin_service.pipeline_stage_statuses(conn)}
        assert statuses["translate"]["status"] == "done"
        assert statuses["translate"]["ran_at"] is not None


def test_run_pipeline_stage_enrichment_dispatches_with_correct_stage_set(monkeypatch: pytest.MonkeyPatch) -> None:
    enrichment = _require_enrichment()
    captured = {}

    def _fake(conn, *, stages, **kwargs):
        captured["stages"] = stages
        return _StubEnrichmentResult(enriched=7, skipped_stages=())

    monkeypatch.setattr(enrichment, "run_llm_enrichment", _fake)
    with migrated_schema() as conn:
        result = admin_service.run_pipeline_stage(conn, "judge")
        assert captured["stages"] == {"judge"}
        assert result["status"] == "done"
        assert "7" in result["detail"]


def test_run_pipeline_stage_records_error_status_and_reraises(monkeypatch: pytest.MonkeyPatch) -> None:
    enrichment = _require_enrichment()

    def _boom(conn, *, stages, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(enrichment, "run_llm_enrichment", _boom)
    with migrated_schema() as conn:
        with pytest.raises(RuntimeError):
            admin_service.run_pipeline_stage(conn, "dedup")

        statuses = {s["key"]: s for s in admin_service.pipeline_stage_statuses(conn)}
        assert statuses["dedup"]["status"] == "error"
        assert "provider exploded" in statuses["dedup"]["detail"]


def test_run_pipeline_all_isolates_one_stage_failure_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    enrichment = _require_enrichment()

    def _selective_fail(conn, *, stages, **kwargs):
        if stages == {"judge"}:
            raise RuntimeError("judge stage broke")
        return _StubEnrichmentResult(enriched=1, skipped_stages=())

    monkeypatch.setattr(enrichment, "run_llm_enrichment", _selective_fail)
    monkeypatch.setattr(
        admin_service,
        "_run_bodies_stage",
        lambda conn, **kw: {"attempted": 0, "fetched": 0, "failed": 0, "skipped_no_plugin": 0},
    )

    with migrated_schema() as conn:
        results = admin_service.run_pipeline_all(conn)
        by_stage = {r["stage"]: r for r in results}
        assert len(results) == 5
        assert by_stage["judge"]["status"] == "error"
        assert by_stage["translate"]["status"] == "done"
        assert by_stage["topic"]["status"] == "done"


class _FakeBodySource(NewsSource):
    key = "fake_body_outlet"
    display_name = "Body Outlet"

    def __init__(self) -> None:
        pass

    def iter_feed_items(self) -> list[FeedItem]:
        return []

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="the full article text")


def test_run_pipeline_stage_bodies_fetches_and_marks_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.sources.registry as registry_module

    # _run_bodies_stage (unlike probe_source/diagnose_source) reads the
    # registry directly via app.sources.registry.all_sources(), not via
    # admin._plugin_for_source_name -- patch that instead.
    monkeypatch.setattr(registry_module, "all_sources", lambda: {"fake_body_outlet": _FakeBodySource})
    monkeypatch.setattr(admin_service, "_ensure_registry_loaded", lambda: None)
    with migrated_schema() as conn:
        admin_service.create_source(conn, name="Body Outlet", methods=["rss"])
        article_id = insert_article(
            conn,
            url="https://x.test/body",
            url_norm="https://x.test/body",
            source="Body Outlet",
            title="Headline",
            day=dt.date.today(),
        )
        conn.commit()

        result = admin_service.run_pipeline_stage(conn, "bodies")
        assert result["status"] == "done"
        assert "fetched 1" in result["detail"]

        row = conn.execute("SELECT body_status FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert row["body_status"] == "fetched"
        body = conn.execute("SELECT content_text FROM article_bodies WHERE article_id = %s", (article_id,)).fetchone()
        assert body["content_text"] == "the full article text"


# ── Router-level ──────────────────────────────────────────────────────


def test_router_health_overview_sources_runs_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        import app.api.routers.health as health_router_module

        admin_service.create_source(conn, name="A", methods=["rss"])
        _insert_run(conn, details={"A": {"status": "ok", "fetched": 5, "inserted": 5, "error": None}}, total_matched=2)
        conn.commit()

        monkeypatch.setattr(health_router_module, "get_connection", lambda: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            overview = client.get("/health/overview")
            assert overview.status_code == 200
            assert overview.json()["healthy"] == 1

            sources = client.get("/health/sources")
            assert sources.status_code == 200
            assert sources.json()[0]["name"] == "A"

            runs = client.get("/health/runs")
            assert runs.status_code == 200
            assert len(runs.json()) == 1


def test_router_pipeline_stage_run_and_run_all(monkeypatch: pytest.MonkeyPatch) -> None:
    enrichment = _require_enrichment()
    with migrated_schema() as conn:
        import app.api.routers.health as health_router_module

        monkeypatch.setattr(health_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        monkeypatch.setattr(
            enrichment, "run_llm_enrichment", lambda conn, *, stages, **kw: _StubEnrichmentResult(enriched=0)
        )
        monkeypatch.setattr(
            admin_service,
            "_run_bodies_stage",
            lambda conn, **kw: {"attempted": 0, "fetched": 0, "failed": 0, "skipped_no_plugin": 0},
        )

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            run_one = client.post("/health/pipeline/stages/translate/run")
            assert run_one.status_code == 200
            assert run_one.json()["status"] == "done"

            bad_stage = client.post("/health/pipeline/stages/not_a_stage/run")
            assert bad_stage.status_code == 422

            run_all = client.post("/health/pipeline/run-all")
            assert run_all.status_code == 200
            assert len(run_all.json()) == 5


# ── Fetch (P1) trigger + live progress (BUGFIX: "Run all stages" never
# actually fetched anything -- see app.services.admin's "Fetch (P1)
# trigger" module section and app/api/routers/health.py's own docstring
# for the full writeup). `app.services.collector.run_collection` itself
# is monkeypatched in every test below -- it's already covered end-to-end
# against fake NewsSource plugins in tests/test_p1_collector.py; what
# THESE tests prove is the NEW wiring around it: that a request to start
# a fetch returns immediately without blocking for the real fetch
# duration, that progress genuinely accumulates while a fetch is still
# "running" (not just before/after), and that the background thread's
# own connection handling and error path work. `admin_service._fetch_state`
# is process-global, so every test resets it first -- these tests would
# otherwise leak state into each other (and into any other test module
# that happens to run in the same pytest-xdist worker).
# ──────────────────────────────────────────────────────────────────────


def _reset_fetch_state() -> None:
    admin_service._fetch_state.clear()
    admin_service._fetch_state.update(status="idle")


def test_fetch_status_is_idle_before_any_run() -> None:
    _reset_fetch_state()
    status = admin_service.fetch_status()
    assert status["status"] == "idle"
    assert status["sources"] == []
    assert status["completed"] == 0
    assert status["total_sources"] == 0


def test_start_fetch_run_returns_immediately_while_the_real_run_is_still_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact bug this mission fixes, as a direct assertion: starting
    a fetch must not hold the caller (an HTTP request, here the direct
    service call) hostage for the full run duration -- `start_fetch_run`
    returns a "running" snapshot while `_fake_run_collection` below is
    still deliberately blocked on an Event, proving the real work is
    happening on a separate thread.
    """
    import app.db.connection as db_connection_module
    import app.services.collector as collector_module
    import app.sources.registry as registry_module

    _reset_fetch_state()
    release = threading.Event()
    calls = []

    def _fake_run_collection(conn, *, source_classes, trigger, progress_cb=None):
        calls.append(trigger)
        release.wait(timeout=5)
        from app.services.collector import CollectionRunResult

        return CollectionRunResult(sources=[], total_fetched=0, total_inserted=0, total_duplicate=0, run_id=7)

    with migrated_schema() as conn:
        monkeypatch.setattr(collector_module, "run_collection", _fake_run_collection)
        monkeypatch.setattr(registry_module, "all_sources", lambda: {"fake": object})
        monkeypatch.setattr(registry_module, "load_all", lambda: None)
        monkeypatch.setattr(db_connection_module, "get_connection", lambda *a, **kw: contextlib.nullcontext(conn))

        try:
            snapshot = admin_service.start_fetch_run(trigger="manual")
            assert snapshot["status"] == "running"
            assert snapshot["trigger"] == "manual"
            assert snapshot["started_at"] is not None

            # A second trigger while the first is still running must NOT
            # start a redundant second background run.
            again = admin_service.start_fetch_run(trigger="manual")
            assert again["status"] == "running"
        finally:
            release.set()  # let the background thread finish so it doesn't leak past the test
            for _ in range(50):
                if admin_service.fetch_status()["status"] != "running":
                    break
                time.sleep(0.05)

        assert calls == ["manual"]  # only one real run_collection call, not two
        final = admin_service.fetch_status()
        assert final["status"] == "done"
        assert final["run_id"] == 7


def test_fetch_progress_cb_accumulates_sources_incrementally(monkeypatch: pytest.MonkeyPatch) -> None:
    """The actual live-progress mechanism (progress_cb, threaded through
    from app.services.collector.run_collection): each source's result
    must be visible via fetch_status() as soon as it's reported, not
    batched up and revealed only once the whole run finishes.
    """
    import app.db.connection as db_connection_module
    import app.services.collector as collector_module
    import app.sources.registry as registry_module
    from app.services.collector import CollectionRunResult, SourceResult

    _reset_fetch_state()
    seen_after_first_source: dict = {}

    def _fake_run_collection(conn, *, source_classes, trigger, progress_cb=None):
        first = SourceResult(source_key="a", display_name="Source A", status="ok", fetched=5, inserted=3)
        second = SourceResult(source_key="b", display_name="Source B", status="failed", fetched=0, error="boom")
        assert progress_cb is not None
        progress_cb(first)
        # Snapshot mid-run, BEFORE the second source reports in -- proves
        # fetch_status() reflects partial progress, not an all-or-nothing
        # final state.
        seen_after_first_source.update(admin_service.fetch_status())
        progress_cb(second)
        return CollectionRunResult(
            sources=[first, second], total_fetched=5, total_inserted=3, total_duplicate=0, run_id=11
        )

    with migrated_schema() as conn:
        monkeypatch.setattr(collector_module, "run_collection", _fake_run_collection)
        monkeypatch.setattr(registry_module, "all_sources", lambda: {"a": object, "b": object})
        monkeypatch.setattr(registry_module, "load_all", lambda: None)
        monkeypatch.setattr(db_connection_module, "get_connection", lambda *a, **kw: contextlib.nullcontext(conn))

        admin_service.start_fetch_run(trigger="manual")
        for _ in range(50):
            if admin_service.fetch_status()["status"] != "running":
                break
            time.sleep(0.05)

    assert seen_after_first_source["completed"] == 1
    assert seen_after_first_source["sources"][0]["name"] == "Source A"
    assert seen_after_first_source["status"] == "running"

    final = admin_service.fetch_status()
    assert final["status"] == "done"
    assert final["completed"] == 2
    assert {s["name"] for s in final["sources"]} == {"Source A", "Source B"}
    assert final["total_inserted"] == 3


def test_fetch_run_records_error_status_on_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.db.connection as db_connection_module
    import app.services.collector as collector_module
    import app.sources.registry as registry_module

    _reset_fetch_state()

    def _boom(conn, *, source_classes, trigger, progress_cb=None):
        raise RuntimeError("simulated collector crash")

    with migrated_schema() as conn:
        monkeypatch.setattr(collector_module, "run_collection", _boom)
        monkeypatch.setattr(registry_module, "all_sources", lambda: {"a": object})
        monkeypatch.setattr(registry_module, "load_all", lambda: None)
        monkeypatch.setattr(db_connection_module, "get_connection", lambda *a, **kw: contextlib.nullcontext(conn))

        admin_service.start_fetch_run(trigger="manual")
        for _ in range(50):
            if admin_service.fetch_status()["status"] == "running":
                time.sleep(0.05)
            else:
                break

    final = admin_service.fetch_status()
    assert final["status"] == "error"
    assert "simulated collector crash" in final["error"]


def test_router_fetch_run_and_status_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.db.connection as db_connection_module
    import app.services.collector as collector_module
    import app.sources.registry as registry_module
    from app.services.collector import CollectionRunResult

    _reset_fetch_state()
    release = threading.Event()

    def _fake_run_collection(conn, *, source_classes, trigger, progress_cb=None):
        release.wait(timeout=5)
        return CollectionRunResult(sources=[], total_fetched=0, total_inserted=0, total_duplicate=0, run_id=42)

    with migrated_schema() as conn:
        monkeypatch.setattr(collector_module, "run_collection", _fake_run_collection)
        monkeypatch.setattr(registry_module, "all_sources", lambda: {"a": object})
        monkeypatch.setattr(registry_module, "load_all", lambda: None)
        monkeypatch.setattr(db_connection_module, "get_connection", lambda *a, **kw: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        try:
            with TestClient(app) as client:
                started = client.post("/health/pipeline/fetch/run")
                assert started.status_code == 200
                assert started.json()["status"] == "running"
                assert started.json()["trigger"] == "manual"

                polled = client.get("/health/pipeline/fetch/status")
                assert polled.status_code == 200
                assert polled.json()["status"] == "running"
        finally:
            release.set()
            for _ in range(50):
                if admin_service.fetch_status()["status"] != "running":
                    break
                time.sleep(0.05)

        with TestClient(app) as client:
            done = client.get("/health/pipeline/fetch/status")
            assert done.json()["status"] == "done"
            assert done.json()["run_id"] == 42


def test_router_match_run_dispatches_to_rematch_today(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        import app.api.routers.health as health_router_module

        admin_service.create_keyword(conn, term_en="flood")
        insert_article(
            conn,
            url="https://x.test/flood-story",
            url_norm="https://x.test/flood-story",
            source="X",
            title="Major flood hits the region",
            day=dt.date.today(),
        )
        conn.commit()

        monkeypatch.setattr(health_router_module, "get_connection", lambda: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            resp = client.post("/health/pipeline/match/run")
            assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────
# Consolidated run (BUGFIX-NAV mission): fetch -> match -> enrich, all
# in ONE background thread, entirely server-side -- the fix for "Run
# collection" silently abandoning match/enrich if an admin navigates
# away while fetch is still running (see app.services.admin's
# "Consolidated run" module section for the full bug). `_run_state` is
# process-global, same reset-before-each-test discipline as
# `_fetch_state` above.
# ──────────────────────────────────────────────────────────────────────


def _reset_run_state() -> None:
    admin_service._run_state.clear()
    admin_service._run_state.update(status="idle", phase="idle")


def test_run_status_is_idle_before_any_run() -> None:
    _reset_run_state()
    status = admin_service.run_status()
    assert status["status"] == "idle"
    assert status["phase"] == "idle"
    assert status["fetch"] is None
    assert status["match"] is None
    assert status["enrich"] is None


def test_start_run_requires_at_least_one_phase() -> None:
    _reset_run_state()
    with pytest.raises(ValidationError):
        admin_service.start_run(fetch=False, match=False, enrich=False)


def test_start_run_completes_match_and_enrich_with_zero_further_calls_after_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact bug this mission fixes, as a direct assertion: this test
    calls `start_run()` exactly ONCE and then never calls any
    phase-specific function again (no runFetchPhase/runMatchPhase
    equivalent, no second start_run) -- simulating an admin navigating
    away the instant the call returns. Match and enrich must still run
    to completion entirely on their own, proving the fetch -> match ->
    enrich sequencing decision now lives in the background thread, not
    in whatever called start_run().
    """
    import app.db.connection as db_connection_module
    import app.services.collector as collector_module
    import app.sources.registry as registry_module
    from app.services.collector import CollectionRunResult

    _require_enrichment()  # enrich runs the real (safe, no-provider-configured) stages
    _reset_run_state()
    release = threading.Event()

    def _fake_run_collection(conn, *, source_classes, trigger, progress_cb=None):
        release.wait(timeout=5)
        return CollectionRunResult(sources=[], total_fetched=0, total_inserted=0, total_duplicate=0, run_id=99)

    with migrated_schema() as conn:
        admin_service.create_keyword(conn, term_en="flood")
        insert_article(
            conn,
            url="https://x.test/flood-run",
            url_norm="https://x.test/flood-run",
            source="X",
            title="Major flood hits the region",
            day=dt.date.today(),
        )
        conn.commit()

        monkeypatch.setattr(collector_module, "run_collection", _fake_run_collection)
        monkeypatch.setattr(registry_module, "all_sources", lambda: {})
        monkeypatch.setattr(registry_module, "load_all", lambda: None)
        monkeypatch.setattr(db_connection_module, "get_connection", lambda *a, **kw: contextlib.nullcontext(conn))

        try:
            snapshot = admin_service.start_run(fetch=True, match=True, enrich=True, trigger="manual")
            assert snapshot["status"] == "running"
            # fetch may already have flipped to "running" by the time this
            # snapshot was taken (a race with the background thread, not a
            # bug) -- what matters is match/enrich have not started.
            assert snapshot["fetch"]["status"] in ("pending", "running")
            assert snapshot["match"]["status"] == "pending"
            assert len(snapshot["enrich"]["stages"]) == 5

            # Fetch is still deliberately blocked -- match/enrich cannot
            # have started yet. This is the "navigated away" moment: no
            # further call against this run happens until the `finally`
            # block's polling loop, which only OBSERVES completion.
            time.sleep(0.2)
            still_blocked = admin_service.run_status()
            assert still_blocked["status"] == "running"
            assert still_blocked["match"]["status"] == "pending"
        finally:
            release.set()  # let fetch finish so it doesn't leak past the test
            for _ in range(100):
                if admin_service.run_status()["status"] != "running":
                    break
                time.sleep(0.05)

        final = admin_service.run_status()
        assert final["status"] == "done"
        assert final["fetch"]["status"] == "done"
        assert final["fetch"]["run_id"] == 99
        assert final["match"]["status"] == "done"
        assert final["match"]["matched"] == 1
        assert final["enrich"]["status"] == "done"
        assert len(final["enrich"]["stages"]) == 5
        assert {s["status"] for s in final["enrich"]["stages"]} == {"done"}


def test_start_run_isolates_a_failed_fetch_so_match_and_enrich_still_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One phase failing must not block the next -- same per-unit
    isolation philosophy run_pipeline_all already applies per-stage.
    """
    import app.db.connection as db_connection_module
    import app.services.collector as collector_module
    import app.sources.registry as registry_module

    _require_enrichment()
    _reset_run_state()

    def _boom(conn, *, source_classes, trigger, progress_cb=None):
        raise RuntimeError("simulated collector crash")

    with migrated_schema() as conn:
        admin_service.create_keyword(conn, term_en="flood")
        insert_article(
            conn,
            url="https://x.test/flood-isolated",
            url_norm="https://x.test/flood-isolated",
            source="X",
            title="Major flood hits the region",
            day=dt.date.today(),
        )
        conn.commit()

        monkeypatch.setattr(collector_module, "run_collection", _boom)
        monkeypatch.setattr(registry_module, "all_sources", lambda: {})
        monkeypatch.setattr(registry_module, "load_all", lambda: None)
        monkeypatch.setattr(db_connection_module, "get_connection", lambda *a, **kw: contextlib.nullcontext(conn))

        admin_service.start_run(fetch=True, match=True, enrich=True, trigger="manual")
        for _ in range(100):
            if admin_service.run_status()["status"] != "running":
                break
            time.sleep(0.05)

    final = admin_service.run_status()
    assert final["status"] == "done"  # the overall run still finishes despite fetch's crash
    assert final["fetch"]["status"] == "error"
    assert "simulated collector crash" in final["fetch"]["error"]
    assert final["match"]["status"] == "done"
    assert final["match"]["matched"] == 1
    assert final["enrich"]["status"] == "done"


def test_start_run_while_already_running_returns_existing_snapshot_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.db.connection as db_connection_module
    import app.services.collector as collector_module
    import app.sources.registry as registry_module
    from app.services.collector import CollectionRunResult

    _reset_run_state()
    release = threading.Event()
    calls = []

    def _fake_run_collection(conn, *, source_classes, trigger, progress_cb=None):
        calls.append(trigger)
        release.wait(timeout=5)
        return CollectionRunResult(sources=[], total_fetched=0, total_inserted=0, total_duplicate=0, run_id=5)

    with migrated_schema() as conn:
        monkeypatch.setattr(collector_module, "run_collection", _fake_run_collection)
        monkeypatch.setattr(registry_module, "all_sources", lambda: {})
        monkeypatch.setattr(registry_module, "load_all", lambda: None)
        monkeypatch.setattr(db_connection_module, "get_connection", lambda *a, **kw: contextlib.nullcontext(conn))

        try:
            admin_service.start_run(fetch=True, match=False, enrich=False, trigger="manual")
            # A second, differently-shaped request while the first is
            # still running must NOT start a redundant second run --
            # same "one run at a time" contract as start_fetch_run.
            again = admin_service.start_run(fetch=True, match=True, enrich=True, trigger="manual")
            assert again["status"] == "running"
            assert again["match"] is None  # unchanged from the FIRST call's request
        finally:
            release.set()
            for _ in range(100):
                if admin_service.run_status()["status"] != "running":
                    break
                time.sleep(0.05)

        assert calls == ["manual"]  # only one real run_collection call, not two


def test_router_run_start_and_status_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.db.connection as db_connection_module
    import app.services.collector as collector_module
    import app.sources.registry as registry_module
    from app.services.collector import CollectionRunResult

    _reset_run_state()
    release = threading.Event()

    def _fake_run_collection(conn, *, source_classes, trigger, progress_cb=None):
        release.wait(timeout=5)
        return CollectionRunResult(sources=[], total_fetched=0, total_inserted=0, total_duplicate=0, run_id=77)

    with migrated_schema() as conn:
        monkeypatch.setattr(collector_module, "run_collection", _fake_run_collection)
        monkeypatch.setattr(registry_module, "all_sources", lambda: {})
        monkeypatch.setattr(registry_module, "load_all", lambda: None)
        monkeypatch.setattr(db_connection_module, "get_connection", lambda *a, **kw: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        try:
            with TestClient(app) as client:
                started = client.post("/health/pipeline/run", json={"fetch": True, "match": False, "enrich": False})
                assert started.status_code == 200
                body = started.json()
                assert body["status"] == "running"
                assert body["match"] is None

                polled = client.get("/health/pipeline/run/status")
                assert polled.status_code == 200
                assert polled.json()["status"] == "running"
        finally:
            release.set()
            for _ in range(100):
                if admin_service.run_status()["status"] != "running":
                    break
                time.sleep(0.05)

        with TestClient(app) as client:
            done = client.get("/health/pipeline/run/status")
            body = done.json()
            assert body["status"] == "done"
            assert body["fetch"]["run_id"] == 77


def test_router_run_start_rejects_no_phase_selected() -> None:
    _reset_run_state()
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    with TestClient(app) as client:
        resp = client.post("/health/pipeline/run", json={"fetch": False, "match": False, "enrich": False})
        assert resp.status_code == 422

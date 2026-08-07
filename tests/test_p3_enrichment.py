"""`app.services.enrichment` — the P3 stage orchestrator, the faithful
successor to `collection_service.run_llm_enrichment()` (NOT
`pipeline.enrich()`, dead code — mission fact (a)).

Two layers of tests:

  1. Pure-logic unit tests (no DB) for the ported per-article functions
     (`stage_translate`, `stage_llm_verify_groups`,
     `stage_dedup_embeddings`, `embed_prefold`, `_select_where`,
     `_looks_english`, `_norm_label`) — these operate on plain dicts,
     independent of Postgres.
  2. End-to-end integration tests against real Postgres
     (`app.db.testing.migrated_schema`), seeding real `articles` rows
     and running `run_llm_enrichment` with `_try_build_client`
     monkeypatched to hand back an `LLMClient` wrapping
     `FakeLLMProvider` — proving the actual SQL (row selection, batch
     writes, standalone-rerun WHERE clauses) matches p3-contract.md §6.

No LLM tokens are spent anywhere in this file.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.services.enrichment import (
    EnrichmentResult,
    _looks_english,
    _norm_label,
    _select_where,
    embed_prefold,
    run_llm_enrichment,
    stage_dedup_embeddings,
    stage_llm_verify_groups,
    stage_translate,
)
from app.services.llm.embedding import UnavailableEmbeddingBackend
from app.services.llm.provider import LLMClient
from tests.helpers.fake_llm import FakeLLMProvider, cluster_topics_response, judge_batch_response, verify_group_response

# ── pure-logic tests (no DB) ──────────────────────────────────────────────


def test_looks_english_matches_pipeline_threshold() -> None:
    assert _looks_english("A perfectly normal English headline") is True
    assert _looks_english("বাংলা শিরোনাম সম্পূর্ণরূপে") is False
    assert _looks_english("") is False
    # mixed, mostly-Latin (>0.6 ascii ratio) should read as English
    assert _looks_english("GP launches বাংলা service") is True


def test_norm_label_collapses_punctuation_and_case() -> None:
    assert _norm_label("Middle East / oil") == "middle east oil"
    assert _norm_label("MIDDLE EAST/OIL") == "middle east oil"
    assert _norm_label("  Middle   East / Oil  ") == "middle east oil"


def _where(*, full_run: bool = False, bodies=False, dedup=False, judge=False, translate=False, topic=False) -> str:
    return _select_where(
        full_run=full_run, do_bodies=bodies, do_dedup=dedup, do_judge=judge, do_translate=translate, do_topic=topic
    )


def test_select_where_full_run_is_unscored_pending() -> None:
    where = _where(full_run=True, dedup=True, judge=True, translate=True, topic=True)
    assert where == "day=%s AND review_status='pending' AND llm_score IS NULL"


def test_select_where_standalone_translate_only() -> None:
    where = _where(translate=True)
    assert where == "day=%s AND review_status='pending' AND (title_en IS NULL OR title_en='')"


def test_select_where_standalone_dedup_pulls_everything_pending() -> None:
    where = _where(dedup=True)
    assert where == "day=%s AND review_status='pending'"


def test_select_where_standalone_bodies() -> None:
    where = _where(bodies=True)
    # content_text lives in the child table `article_bodies`, not
    # `articles` (migrations/0004) — referenced here as `ab.content_text`
    # against the LEFT JOIN `run_llm_enrichment` always uses.
    assert where == "day=%s AND review_status='pending' AND (ab.content_text IS NULL OR ab.content_text='')"


def test_select_where_standalone_topic_pulls_pending_as_a_nonempty_selection() -> None:
    where = _where(topic=True)
    assert where == "day=%s AND review_status='pending'"


def test_select_where_standalone_judge_is_unscored_pending() -> None:
    where = _where(judge=True)
    assert where == "day=%s AND review_status='pending' AND llm_score IS NULL"


def test_stage_translate_skips_already_translated() -> None:
    articles = [{"title": "x", "title_en": "cached"}]
    result = stage_translate(articles, client=None)
    assert result[0]["title_en"] == "cached"


def test_stage_translate_english_title_is_free_no_client_needed() -> None:
    articles = [{"title": "A perfectly normal English headline"}]
    result = stage_translate(articles, client=None)
    assert result[0]["title_en"] == "A perfectly normal English headline"


def test_stage_translate_non_english_with_client_calls_translate_title() -> None:
    fake = FakeLLMProvider(responses=lambda p, s: "Translated Title")
    client = LLMClient(transport=fake, name="fake")
    articles = [{"title": "বাংলা শিরোনাম"}]
    result = stage_translate(articles, client=client)
    assert result[0]["title_en"] == "Translated Title"


def test_stage_translate_non_english_no_client_falls_back_to_original() -> None:
    articles = [{"title": "বাংলা শিরোনাম"}]
    result = stage_translate(articles, client=None)
    assert result[0]["title_en"] == "বাংলা শিরোনাম"


def test_stage_translate_progress_callback_fires_per_article() -> None:
    calls = []
    articles = [{"title": "English one"}, {"title": "English two"}]
    stage_translate(articles, client=None, progress=lambda done, total, step: calls.append((done, total, step)))
    assert calls == [(1, 2, "Translating"), (2, 2, "Translating")]


def test_stage_dedup_embeddings_unavailable_backend_passes_through_unchanged() -> None:
    articles = [{"id": 1, "title": "A"}, {"id": 2, "dup_group": 7, "is_primary": False, "also_in": "x"}]
    result = stage_dedup_embeddings(articles, UnavailableEmbeddingBackend())
    assert result[0]["dup_group"] is None
    assert result[0]["is_primary"] is True
    assert result[0]["also_in"] == ""
    # article 2 already had explicit values — setdefault must NOT overwrite them
    assert result[1]["dup_group"] == 7
    assert result[1]["is_primary"] is False
    assert result[1]["also_in"] == "x"


def test_stage_dedup_embeddings_empty_list_is_a_noop() -> None:
    assert stage_dedup_embeddings([], UnavailableEmbeddingBackend()) == []


def test_stage_llm_verify_groups_no_client_is_a_noop() -> None:
    articles = [{"id": 1, "dup_group": 0}, {"id": 2, "dup_group": 0}]
    result = stage_llm_verify_groups(articles, client=None)
    assert result == articles


def test_stage_llm_verify_groups_singleton_groups_are_untouched() -> None:
    fake = FakeLLMProvider()
    client = LLMClient(transport=fake, name="fake")
    articles = [{"id": 1, "dup_group": None, "title": "Solo story"}]
    result = stage_llm_verify_groups(articles, client)
    assert result[0]["is_primary"] is True
    assert result[0]["also_in"] == ""
    assert len(fake.call_log) == 0  # never called — nothing to verify


def test_stage_llm_verify_groups_splits_a_wrongly_merged_group() -> None:
    fake = FakeLLMProvider(responses=lambda p, s: verify_group_response([0, 1, 0]))
    client = LLMClient(transport=fake, name="fake")
    articles = [
        {"id": 1, "dup_group": 5, "title": "Story A v1", "title_en": "Story A v1", "source": "S1"},
        {"id": 2, "dup_group": 5, "title": "Story B", "title_en": "Story B", "source": "S2"},
        {"id": 3, "dup_group": 5, "title": "Story A v2", "title_en": "Story A v2", "source": "S3"},
    ]
    result = stage_llm_verify_groups(articles, client)
    # articles 1 and 3 (sub-id 0) share a fresh group; article 2 (sub-id 1) gets its own
    assert result[0]["dup_group"] == result[2]["dup_group"]
    assert result[1]["dup_group"] != result[0]["dup_group"]
    # each new group's is_primary/also_in was recomputed
    primaries = [a for a in result if a["is_primary"]]
    assert len(primaries) == 2  # one per distinct final group


def test_stage_llm_verify_groups_chunks_at_12() -> None:
    fake = FakeLLMProvider(responses=lambda p, s: verify_group_response([0] * 12))
    client = LLMClient(transport=fake, name="fake")
    # 14 members -> chunks of 12 + 2 -> BOTH chunks have >=2 members -> 2 calls
    # (a remaining chunk of exactly 1 is skipped entirely, per
    # `if len(chunk) < 2: continue` — a singleton can't be "verified" —
    # covered separately by test_stage_llm_verify_groups_singleton_groups_are_untouched
    # and the 13-member case below).
    articles = [{"id": i, "dup_group": 9, "title": f"Story {i}"} for i in range(14)]
    stage_llm_verify_groups(articles, client)
    assert len(fake.call_log) == 2


def test_stage_llm_verify_groups_trailing_singleton_chunk_is_not_called() -> None:
    fake = FakeLLMProvider(responses=lambda p, s: verify_group_response([0] * 12))
    client = LLMClient(transport=fake, name="fake")
    # 13 members -> chunks of 12 + 1 -> the trailing 1-member chunk is
    # skipped (nothing to verify in a chunk of one) -> only 1 call.
    articles = [{"id": i, "dup_group": 9, "title": f"Story {i}"} for i in range(13)]
    stage_llm_verify_groups(articles, client)
    assert len(fake.call_log) == 1


def test_embed_prefold_unavailable_backend_returns_members_unchanged() -> None:
    members = [{"id": 1}, {"id": 2}]
    leads, fold_map = embed_prefold(members, 0.82, UnavailableEmbeddingBackend())
    assert leads == members
    assert fold_map == {}


def test_embed_prefold_fewer_than_two_members_short_circuits() -> None:
    members = [{"id": 1}]
    leads, fold_map = embed_prefold(members, 0.82, UnavailableEmbeddingBackend())
    assert leads == members
    assert fold_map == {}


# ── integration tests against real Postgres ───────────────────────────────

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

TODAY = dt.date(2026, 8, 7)


def _seed_article(conn, **overrides) -> int:
    fields = {
        "url": f"https://example.com/{overrides.get('title', 'x')}",
        "url_norm": f"https://example.com/{overrides.get('title', 'x')}",
        "source": "Example",
        "title": "Headline",
        "day": TODAY,
        "review_status": "pending",
    }
    fields.update(overrides)
    article_id = insert_article(conn, **fields)
    assert article_id is not None
    return article_id


def _fake_client_factory(monkeypatch: pytest.MonkeyPatch, responses_by_feature: dict[str, object]) -> None:
    """Monkeypatch `_try_build_client` so `run_llm_enrichment` never
    touches `provider_configs`/routing — each feature gets an
    `LLMClient` wrapping a `FakeLLMProvider` with the given canned
    response function (or `None` to simulate "feature unconfigured").
    """
    import app.services.enrichment as enrichment_module

    def fake_try_build(conn, feature):
        responses = responses_by_feature.get(feature, "__unset__")
        if responses is None:
            return None
        if responses == "__unset__":
            fake = FakeLLMProvider()
        else:
            fake = FakeLLMProvider(responses=responses)
        return LLMClient(transport=fake, name=f"fake-{feature}")

    monkeypatch.setattr(enrichment_module, "_try_build_client", fake_try_build)


def test_full_run_translates_dedups_judges_and_tags_topics(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        a1 = _seed_article(conn, title="বাংলা শিরোনাম এক", title_en="", url="u1", url_norm="u1")
        a2 = _seed_article(conn, title="English headline two", url="u2", url_norm="u2")
        conn.commit()

        _fake_client_factory(
            monkeypatch,
            {
                "translation": lambda p, s: "Translated Bengali Headline",
                "dedup_verification": lambda p, s: verify_group_response([0]),
                "scoring": lambda p, s: judge_batch_response([(1, 8, "relevant"), (2, 3, "noise")]),
                "topic_clustering": lambda p, s: cluster_topics_response([(1, "Some Topic"), (2, "Some Topic")]),
            },
        )

        result = run_llm_enrichment(conn, today=TODAY)
        assert isinstance(result, EnrichmentResult)
        assert result.enriched == 2
        assert result.skipped_stages == ()

        rows = {r["id"]: r for r in conn.execute("SELECT * FROM articles WHERE day=%s", (TODAY,)).fetchall()}
        assert rows[a1]["title_en"] == "Translated Bengali Headline"
        assert rows[a2]["title_en"] == "English headline two"  # free, no LLM call needed
        assert rows[a1]["llm_score"] in (8.0, 3.0)  # scored by judge stage
        assert rows[a2]["llm_score"] in (8.0, 3.0)


def test_unconfigured_stage_is_recorded_in_skipped_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        _seed_article(conn, title="English headline", url="u1", url_norm="u1")
        conn.commit()

        unconfigured = {"translation": None, "dedup_verification": None, "scoring": None, "topic_clustering": None}
        _fake_client_factory(monkeypatch, unconfigured)

        result = run_llm_enrichment(conn, today=TODAY)
        # all four features unconfigured -> all four stages recorded as
        # skipped (translate/dedup/judge check their client before
        # running; topic checks its own client too, even though with no
        # llm_score set yet it would also have found <2 primaries).
        assert set(result.skipped_stages) == {"translate", "dedup", "judge", "topic"}


def test_standalone_translate_rerun_only_selects_missing_title_en(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        a1 = _seed_article(conn, title="বাংলা এক", title_en="", url="u1", url_norm="u1")
        a2 = _seed_article(conn, title="বাংলা দুই", title_en="already there", url="u2", url_norm="u2")
        conn.commit()

        _fake_client_factory(monkeypatch, {"translation": lambda p, s: "Fresh Translation"})
        result = run_llm_enrichment(conn, today=TODAY, stages={"translate"})
        assert result.enriched == 1  # only a1 matched the WHERE clause

        rows = {r["id"]: r for r in conn.execute("SELECT * FROM articles WHERE day=%s", (TODAY,)).fetchall()}
        assert rows[a1]["title_en"] == "Fresh Translation"
        assert rows[a2]["title_en"] == "already there"  # untouched — wasn't selected


def test_standalone_judge_rerun_only_scores_unscored(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        a1 = _seed_article(conn, title="One", url="u1", url_norm="u1", llm_score=None)
        a2 = _seed_article(conn, title="Two", url="u2", url_norm="u2")
        conn.execute("UPDATE articles SET llm_score = 7 WHERE id = %s", (a2,))
        conn.commit()

        _fake_client_factory(monkeypatch, {"scoring": lambda p, s: judge_batch_response([(1, 9, "fresh score")])})
        result = run_llm_enrichment(conn, today=TODAY, stages={"judge"})
        assert result.enriched == 1

        rows = {r["id"]: r for r in conn.execute("SELECT * FROM articles WHERE day=%s", (TODAY,)).fetchall()}
        assert rows[a1]["llm_score"] == 9.0
        assert rows[a2]["llm_score"] == 7.0  # untouched, was already scored


def test_judge_batch_size_respects_llm_batch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BATCH", "2")
    with migrated_schema() as conn:
        for i in range(5):
            _seed_article(conn, title=f"Article {i}", url=f"u{i}", url_norm=f"u{i}")
        conn.commit()

        call_count = {"n": 0}

        def _responses(prompt: str, system: str) -> str:
            call_count["n"] += 1
            # echo back a score for every numbered headline in this batch
            n_items = prompt.count("\n") - 1 if prompt.count("\n") > 1 else 1
            return judge_batch_response([(i + 1, 6, "ok") for i in range(n_items)])

        _fake_client_factory(monkeypatch, {"scoring": _responses})
        result = run_llm_enrichment(conn, today=TODAY, stages={"judge"})
        assert result.enriched == 5
        # 5 articles at batch size 2 -> 3 calls (2, 2, 1)
        assert call_count["n"] == 3


def test_should_stop_halts_judge_batch_between_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BATCH", "1")
    with migrated_schema() as conn:
        for i in range(4):
            _seed_article(conn, title=f"Article {i}", url=f"u{i}", url_norm=f"u{i}")
        conn.commit()

        _fake_client_factory(monkeypatch, {"scoring": lambda p, s: judge_batch_response([(1, 5, "ok")])})

        call_state = {"n": 0}

        def stop_after_two():
            return call_state["n"] >= 2

        import app.services.enrichment as enrichment_module

        original_execute = conn.execute

        def counting_execute(sql, *args, **kwargs):
            if "UPDATE articles SET llm_score" in sql:
                call_state["n"] += 1
            return original_execute(sql, *args, **kwargs)

        conn.execute = counting_execute  # type: ignore[method-assign]

        result = enrichment_module.run_llm_enrichment(conn, today=TODAY, stages={"judge"}, should_stop=stop_after_two)
        scored = conn.execute("SELECT count(*) AS n FROM articles WHERE llm_score IS NOT NULL").fetchone()["n"]
        assert scored <= 2  # stopped early — not all 4 got scored
        assert result.enriched == 4  # total selected, regardless of how many were actually processed


def test_topic_clustering_tags_primaries_scoring_5_or_above(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        a1 = _seed_article(conn, title="Relevant one", url="u1", url_norm="u1")
        a2 = _seed_article(conn, title="Relevant two", url="u2", url_norm="u2")
        a3 = _seed_article(conn, title="Irrelevant", url="u3", url_norm="u3")
        conn.execute("UPDATE articles SET llm_score = 8, is_primary = TRUE WHERE id = %s", (a1,))
        conn.execute("UPDATE articles SET llm_score = 6, is_primary = TRUE WHERE id = %s", (a2,))
        conn.execute("UPDATE articles SET llm_score = 2, is_primary = TRUE WHERE id = %s", (a3,))  # below band 5 cutoff
        conn.commit()

        _fake_client_factory(
            monkeypatch,
            {"topic_clustering": lambda p, s: cluster_topics_response([(1, "Shared Topic"), (2, "Shared Topic")])},
        )
        run_llm_enrichment(conn, today=TODAY, stages={"topic"})

        rows = {r["id"]: r for r in conn.execute("SELECT * FROM articles WHERE day=%s", (TODAY,)).fetchall()}
        assert rows[a1]["topic_tag"] == "Shared Topic"
        assert rows[a2]["topic_tag"] == "Shared Topic"
        assert rows[a3]["topic_tag"] == ""  # score 2 < 5, never queried for topic clustering at all


def test_topic_clustering_skipped_when_fewer_than_two_primaries(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        a1 = _seed_article(conn, title="Lone relevant story", url="u1", url_norm="u1")
        conn.execute("UPDATE articles SET llm_score = 9, is_primary = TRUE WHERE id = %s", (a1,))
        conn.commit()

        called = {"n": 0}

        def _count_and_respond(prompt: str, system: str) -> str:
            called["n"] += 1
            return "{}"

        _fake_client_factory(monkeypatch, {"topic_clustering": _count_and_respond})
        run_llm_enrichment(conn, today=TODAY, stages={"topic"})
        assert called["n"] == 0  # never called — fewer than 2 primaries


def test_empty_selection_skips_everything_except_topic() -> None:
    with migrated_schema() as conn:
        result = run_llm_enrichment(conn, today=TODAY, stages={"judge"})
        assert result == EnrichmentResult(enriched=0)

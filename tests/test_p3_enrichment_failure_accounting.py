"""A stage whose every LLM call fails must not report success.

THE INCIDENT THIS FILE EXISTS FOR

An admin added a provider whose base_url already ended in
`/chat/completions`. `OpenAICompatibleTransport` appends that path
itself, so every request 404'd. Translation was routed solely to that
provider with no fallback.

The run finished and the panel said:

    Translate    done - processed 578 article(s)

All 578 articles kept their Bengali headlines. `stage_translate`
substitutes the original title whenever a call fails (correct on its
own: p3-contract.md 7 says never silently drop an article) and nothing
counted the substitutions, so total failure and total success rendered
identically.
"""

from __future__ import annotations

from app.services.enrichment import EnrichmentResult, StageCalls, stage_translate


class _AlwaysFailsClient:
    """Every call raises, the way a 404 base_url behaves."""

    name = "OpenRouter - qwen/qwen3.8-27b"

    def translate_titles_batch(self, titles: list[str]) -> list[str]:
        raise RuntimeError("404 Not Found")

    def translate_title(self, title: str) -> str:
        raise RuntimeError("404 Not Found")


class _WorkingClient:
    name = "mistral - mistral-small-latest"

    def translate_titles_batch(self, titles: list[str]) -> list[str]:
        return [f"EN::{t}" for t in titles]

    def translate_title(self, title: str) -> str:
        return f"EN::{title}"


def _bengali_articles(n: int) -> list[dict]:
    return [{"id": i, "title": f"\u0997\u09b2 \u099f\u09c7\u09b8\u09cd\u099f {i}", "title_en": ""} for i in range(n)]


def test_a_stage_that_fails_every_call_is_counted_as_failed() -> None:
    articles = _bengali_articles(20)
    calls = StageCalls()

    stage_translate(articles, _AlwaysFailsClient(), calls=calls)

    # The neutral default still applies: no article is dropped or blanked.
    assert all(a["title_en"] == a["title"] for a in articles)

    # But the failure is now VISIBLE, which is the whole point.
    assert calls.attempted > 0
    assert calls.succeeded == 0
    assert calls.failed == calls.attempted
    assert "404" in calls.first_error

    result = EnrichmentResult(enriched=len(articles), calls={"translate": calls})
    assert result.wholly_failed_stages() == ("translate",)


def test_a_working_stage_reports_no_failures() -> None:
    articles = _bengali_articles(20)
    calls = StageCalls()

    stage_translate(articles, _WorkingClient(), calls=calls)

    assert all(a["title_en"].startswith("EN::") for a in articles)
    assert calls.failed == 0
    assert calls.succeeded == calls.attempted
    assert EnrichmentResult(enriched=20, calls={"translate": calls}).wholly_failed_stages() == ()


def test_partial_failure_is_not_reported_as_total_failure() -> None:
    """One flaky batch must not condemn a stage that mostly worked."""

    class _FlakyClient:
        name = "flaky"

        def __init__(self) -> None:
            self.n = 0

        def translate_titles_batch(self, titles: list[str]) -> list[str]:
            self.n += 1
            if self.n == 1:
                raise RuntimeError("rate limited")
            return [f"EN::{t}" for t in titles]

        def translate_title(self, title: str) -> str:
            return f"EN::{title}"

    articles = _bengali_articles(200)
    calls = StageCalls()
    stage_translate(articles, _FlakyClient(), calls=calls)

    assert calls.failed >= 1
    assert calls.succeeded >= 1
    # Partial damage is NOT a dead stage; the run should stay green and
    # merely report the failed calls.
    assert EnrichmentResult(enriched=200, calls={"translate": calls}).wholly_failed_stages() == ()


def test_detail_line_names_the_failure_instead_of_only_a_processed_count() -> None:
    """The operator-facing string is where the old bug was visible."""
    from app.services.admin import _enrichment_detail

    dead = StageCalls()
    dead.record(ok=False, error="404 Not Found")
    line = _enrichment_detail(EnrichmentResult(enriched=578, calls={"translate": dead}))

    assert "FAILED" in line
    assert "404 Not Found" in line
    # The misleading half is still present for context, but no longer alone.
    assert "578" in line


def test_no_provider_configured_is_distinct_from_every_call_failing() -> None:
    """Both leave articles untranslated; only one is an outage."""
    articles = _bengali_articles(10)
    calls = StageCalls()
    stage_translate(articles, None, calls=calls)  # no client at all

    assert calls.attempted == 0, "a stage with no provider must not report phantom failures"
    assert EnrichmentResult(enriched=10, calls={"translate": calls}).wholly_failed_stages() == ()


# ── the configuration trap that caused the incident ─────────────────────


def test_a_full_endpoint_as_base_url_is_refused_on_save() -> None:
    """`OpenAICompatibleTransport` appends `/chat/completions` itself, so
    a base_url that already ends in it always 404s. The field was
    labelled only "Base URL, optional" and accepted anything.
    """
    import pytest

    from app.db.testing import migrated_schema
    from app.errors import ValidationError
    from app.services import admin as admin_service

    with migrated_schema() as conn:
        with pytest.raises(ValidationError) as exc:
            admin_service.save_provider_definition(
                conn,
                "openrouter",
                adapter="openai_compatible",
                base_url="https://openrouter.ai/api/v1/chat/completions",
                default_model="qwen/qwen3.8-27b",
            )
        assert "API root" in str(exc.value)
        # The message must show the fix, not just the objection.
        assert "https://openrouter.ai/api/v1" in str(exc.value)

        # The correct form still saves.
        saved = admin_service.save_provider_definition(
            conn,
            "openrouter",
            adapter="openai_compatible",
            base_url="https://openrouter.ai/api/v1",
            default_model="qwen/qwen3.8-27b",
        )
        assert saved["config_problem"] == ""


def test_an_already_stored_bad_row_stays_visible_instead_of_vanishing() -> None:
    """First cut raised from `ProviderDefinition.__post_init__`, which
    made `load_provider_definitions` (it skips rows that fail to
    construct) DROP the stored bad provider from the admin list. The
    provider disappeared from the screen with no explanation and no way
    to fix it: a worse version of the bug being fixed.
    """
    from app.db.testing import migrated_schema
    from app.services import admin as admin_service
    from app.services.llm.provider_registry import ProviderDefinition, save_provider_definitions

    with migrated_schema() as conn:
        save_provider_definitions(
            conn,
            {
                "legacy_bad": ProviderDefinition(
                    provider="legacy_bad",
                    adapter="openai_compatible",
                    base_url="https://vendor.example/v1/chat/completions",
                    default_model="m",
                )
            },
        )
        conn.commit()

        rows = {d["provider"]: d for d in admin_service.list_provider_definitions(conn)}
        assert "legacy_bad" in rows, "a stored bad row must not disappear from the admin list"
        assert "API root" in rows["legacy_bad"]["config_problem"]

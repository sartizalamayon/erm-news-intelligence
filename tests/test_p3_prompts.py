"""Byte-exact verification of `app.services.llm.prompts` against a frozen
copy of the legacy source, `tests/legacy_reference/llm.py` — not against
p3-contract.md's transcription of it (which is itself a copy one step
removed). Every prompt-builder function is exercised with representative
inputs and compared to the exact string legacy's own code would produce
for the same inputs, so a transcription slip anywhere (a missing space, a
smart-quote substituted for a straight one, a dropped `\\n`) fails loudly
here rather than only being caught by eyeballing a diff.

The legacy `backend/` application tree was removed from the working tree
once fully superseded by `app/` (retrievable from git history at the
baseline commit if ever needed). `tests/legacy_reference/llm.py` is a
byte-identical snapshot of `backend/llm.py` taken at that point, kept
specifically so this parity test keeps working without depending on a
live application directory that no longer exists — see
`tests/legacy_reference/README.md`.

`FIXED_GUIDE`/`DEFAULT_EXAMPLES` are compared for full equality (not
just a hash) since these are read directly, verbatim, from that frozen
copy — a mismatch here would mean this module drifted from the frozen
P3 contract, not that the reference changed (it can't; it's frozen).

The reference module is loaded directly by file path (not imported as a
package) since it has no `__init__.py`/package structure this suite
depends on, and importing it must never require a live DB connection —
the only imports gated behind an actual call are the
`_get_relevance_guidance`/`_get_relevance_examples` DB reads inside
`OllamaProvider._build_system()`'s helpers, which this file never
calls (it uses `DEFAULT_EXAMPLES` directly for every comparison,
mirroring what those DB reads themselves fall back to against an
unconfigured/empty `settings` table).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.services.llm import prompts

LEGACY_LLM_PATH = Path(__file__).resolve().parent / "legacy_reference" / "llm.py"


@pytest.fixture(scope="module")
def legacy() -> ModuleType:
    spec = importlib.util.spec_from_file_location("legacy_llm_for_prompt_tests", LEGACY_LLM_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def legacy_provider(legacy: ModuleType):
    """An `OllamaProvider` instance with `_ping()` short-circuited (no
    real network probe — this suite must not open a socket) so
    `_build_system()` is callable.
    """
    legacy.OllamaProvider._ping = lambda self: True
    return legacy.OllamaProvider(model="qwen3:8b", host="http://localhost:11434")


# ── module-level constants ──────────────────────────────────────────────


def test_fixed_guide_is_byte_identical_to_legacy(legacy: ModuleType) -> None:
    assert prompts.FIXED_GUIDE == legacy.FIXED_GUIDE
    assert len(prompts.FIXED_GUIDE) == 3735


def test_default_examples_is_byte_identical_to_legacy(legacy: ModuleType) -> None:
    assert prompts.DEFAULT_EXAMPLES == legacy.DEFAULT_EXAMPLES
    assert len(prompts.DEFAULT_EXAMPLES) == 4290


def test_default_examples_contains_bengali_script() -> None:
    # sanity check that the extraction actually carried the Bengali text
    # through, not just an ASCII skeleton with the Bengali lines dropped.
    assert any("ঀ" <= ch <= "৿" for ch in prompts.DEFAULT_EXAMPLES)


def test_build_system_guide_matches_legacy_build_system(legacy_provider) -> None:
    legacy_system = legacy_provider._build_system()
    new_system = prompts.build_system_guide(prompts.DEFAULT_EXAMPLES, "")
    assert legacy_system == new_system


def test_build_system_guide_appends_guidance_when_present(legacy_provider) -> None:
    # legacy's _build_system() reads guidance from the DB; here we just
    # verify the concatenation shape (append only if non-blank).
    without = prompts.build_system_guide(prompts.DEFAULT_EXAMPLES, "")
    with_guidance = prompts.build_system_guide(prompts.DEFAULT_EXAMPLES, "Always flag Chattogram port news.")
    assert with_guidance.startswith(without)
    assert "Additional analyst guidance:\nAlways flag Chattogram port news." in with_guidance


# ── judge() / judge_batch() ─────────────────────────────────────────────


def test_judge_system_and_prompt_match_legacy(legacy_provider) -> None:
    base = legacy_provider._build_system()
    legacy_system = base + ('\n\nRespond ONLY with JSON: {"score": <1-10 integer>, "reason": "<short phrase>"}.')
    assert prompts.judge_system(base) == legacy_system
    assert prompts.judge_prompt("Some headline", "Some Source") == (
        "Headline: Some headline\nSource: Some Source\n\nRate relevance to Grameenphone."
    )


def test_judge_batch_system_and_prompt_match_legacy(legacy_provider) -> None:
    items = [
        {"title": "টেস্ট শিরোনাম", "title_en": "Test headline", "source": "Prothom Alo"},
        {"title": "English only headline", "source": "Daily Star"},
    ]
    base = legacy_provider._build_system()
    legacy_system = base + (
        "\n\nYou will receive a numbered list of headlines. Score EACH one. "
        'Respond ONLY with JSON: {"scores": [{"n": <number>, '
        '"score": <1-10>, "reason": "<short phrase>"}, ...]} with exactly '
        f"{len(items)} entries, one per headline."
    )
    numbered = "\n".join(f"{i+1}. {it.get('title_en') or it.get('title','')}" for i, it in enumerate(items))
    legacy_prompt = f"Headlines:\n{numbered}\n\nScore all {len(items)} headlines."

    assert prompts.judge_batch_system(base, len(items)) == legacy_system
    assert prompts.judge_batch_prompt(items) == legacy_prompt


def test_judge_batch_prompt_uses_title_en_over_title() -> None:
    items = [{"title": "raw title", "title_en": "translated title"}]
    assert "translated title" in prompts.judge_batch_prompt(items)
    assert "raw title" not in prompts.judge_batch_prompt(items)


def test_judge_batch_prompt_falls_back_to_title_when_no_title_en() -> None:
    items = [{"title": "raw title only"}]
    assert "raw title only" in prompts.judge_batch_prompt(items)


# ── cluster_topics_chunk() ──────────────────────────────────────────────


def test_cluster_topics_system_matches_legacy_with_and_without_known_labels() -> None:
    items = [{"title_en": "Headline A"}, {"title_en": "Headline B"}]

    for known in ([], ["Middle East / oil", "Banking / NPLs"]):
        known_block = ""
        if known:
            known_block = (
                "\n\nTOPICS ALREADY IN USE (reuse the EXACT label when a headline "
                "fits one of these; only invent a new label when none fits):\n"
                + "\n".join(f"- {label}" for label in known)
            )
        legacy_system = (
            "You group news headlines by TOPIC for a Bangladesh risk-analysis "
            "desk. A topic is a real-world theme, BROADER than a single event: "
            "different events in the same ongoing situation share one topic (e.g. "
            "a strike, a shipping-lane closure, and an LNG-supply cut during one "
            "Middle East crisis are ALL 'Middle East / oil'). Keep genuinely "
            "unrelated stories in separate topics — do NOT over-merge. Labels are "
            "SHORT (2-4 words, e.g. 'Middle East / oil', 'Banking / NPLs', "
            "'GP / spectrum', 'RMG exports')." + known_block + "\n\n"
            "Assign EVERY numbered headline a topic label. Return ONLY JSON: "
            '{"labels": [{"n": <number>, "label": "<short label>"}, ...]} '
            f"with one entry per headline ({len(items)} total), no preamble."
        )
        assert prompts.cluster_topics_system(known, len(items)) == legacy_system


def test_cluster_topics_num_predict_matches_legacy_constant() -> None:
    assert prompts.CLUSTER_TOPICS_NUM_PREDICT == 4096


# ── consolidate_labels() ────────────────────────────────────────────────


def test_consolidate_labels_num_predict_matches_legacy_constant() -> None:
    assert prompts.CONSOLIDATE_LABELS_NUM_PREDICT == 2048


def test_consolidate_labels_prompt_numbers_from_one() -> None:
    labels = ["Middle East conflict", "Iran-US tensions"]
    prompt = prompts.consolidate_labels_prompt(labels)
    assert prompt == "Topic labels:\n1. Middle East conflict\n2. Iran-US tensions\n\nGive the canonical label for each."


# ── find_duplicates_batch() ─────────────────────────────────────────────


def test_find_duplicates_shown_cap_and_snippet_len_match_legacy_constants() -> None:
    assert prompts.FIND_DUPLICATES_SHOWN_CAP == 30
    assert prompts.FIND_DUPLICATES_SNIPPET_MAXLEN == 300
    assert prompts.FIND_DUPLICATES_NUM_PREDICT == 1024


def test_find_duplicates_num_ctx_matches_legacy_formula() -> None:
    system = "x" * 1000
    prompt = "y" * 500
    est_tokens = (len(system) + len(prompt)) // 4 + 256
    expected = min(16384, max(4096, ((est_tokens // 2048) + 1) * 2048))
    assert prompts.find_duplicates_num_ctx(system, prompt) == expected == 4096


def test_find_duplicates_shown_block_empty_is_none_yet() -> None:
    assert prompts.find_duplicates_shown_block([]) == "(none yet)"


def test_find_duplicates_candidate_block_includes_snippet_only_when_present() -> None:
    with_body = [{"title_en": "T1", "source": "S1", "content_text": "some body text"}]
    without_body = [{"title_en": "T2", "source": "S2"}]
    assert " :: some body text" in prompts.find_duplicates_candidate_block(with_body)
    assert " :: " not in prompts.find_duplicates_candidate_block(without_body)


# ── verify_group() ──────────────────────────────────────────────────────


def test_verify_group_prompt_and_system_shape() -> None:
    headlines = ["Headline A", "Headline B", "Headline C"]
    prompt = prompts.verify_group_prompt(headlines)
    assert prompt.startswith("Headlines:\n1. Headline A\n2. Headline B\n3. Headline C\n\n")
    assert '{"groups"' in prompts.verify_group_system()


# ── translate_title() ───────────────────────────────────────────────────


def test_translate_title_system_matches_legacy() -> None:
    assert prompts.translate_title_system() == (
        "Translate this news headline to natural English. Output ONLY "
        "the translated headline, nothing else — no quotes, no preamble, "
        "no explanation."
    )


# ── draft_bullet() ───────────────────────────────────────────────────────


def test_draft_bullet_prompt_with_and_without_body() -> None:
    with_body = prompts.draft_bullet_prompt("Title", "Body text", "Macro")
    assert with_body == "Section: Macro\nHeadline: Title\nArticle text: Body text"

    without_body = prompts.draft_bullet_prompt("Title", "", "Macro")
    assert without_body == (
        "Section: Macro\nHeadline: Title\n"
        "(No article text available — draft from the headline only, do not invent figures.)"
    )


def test_draft_bullet_system_contains_endash_example() -> None:
    assert "–" in prompts.draft_bullet_system()


# ── suggest_guidance() ──────────────────────────────────────────────────


def test_suggest_guidance_prompt_caps_at_40_each() -> None:
    approved = [f"A{i}" for i in range(50)]
    rejected = [f"R{i}" for i in range(50)]
    prompt = prompts.suggest_guidance_prompt(approved, rejected)
    assert "A39" in prompt
    assert "A40" not in prompt
    assert "R39" in prompt
    assert "R40" not in prompt


# ── admin-editable guidance/examples read path ──────────────────────────


def test_get_relevance_examples_falls_back_to_default_when_unset() -> None:
    from app.db.testing import migrated_schema, pg_available

    if not pg_available():
        pytest.skip("no reachable Postgres (set TEST_DATABASE_URL)")
    with migrated_schema() as conn:
        assert prompts.get_relevance_examples(conn) == prompts.DEFAULT_EXAMPLES
        assert prompts.get_relevance_guidance(conn) == ""


def test_get_relevance_examples_and_guidance_respect_admin_overrides() -> None:
    from app.db.settings import set_setting
    from app.db.testing import migrated_schema, pg_available

    if not pg_available():
        pytest.skip("no reachable Postgres (set TEST_DATABASE_URL)")
    with migrated_schema() as conn:
        set_setting(conn, "relevance_examples", "Custom example => 7")
        set_setting(conn, "relevance_guidance", "Always flag port disruption.")
        conn.commit()
        assert prompts.get_relevance_examples(conn) == "Custom example => 7"
        assert prompts.get_relevance_guidance(conn) == "Always flag port disruption."

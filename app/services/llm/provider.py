"""`LLMClient` — the common interface over Groq and Google AI Studio
(DoD #1). Every method here is a faithful, prompt-and-parsing-level port
of one `backend/llm.py` method (cited per method), built on top of:

  * `app.services.llm.prompts` — the frozen prompt text, byte-verified
    against `backend/llm.py` in `tests/test_p3_prompts.py`.
  * `app.services.llm.parsing` — the frozen parse/clamp/fallback logic.
  * `app.services.llm.transport.Transport` — the swappable choke point
    (`tests/helpers/fake_llm.py`'s `FakeLLMProvider` in tests, a real
    `GroqTransport`/`GeminiTransport` — possibly wrapped in
    `app.services.llm.failover.FailoverTransport` for automatic
    failover — in production).

`LLMClient` itself never decides WHICH provider/model it's talking to —
that's `app.services.llm.registry`'s job, informed by
`app.services.llm.routing`. This class only knows how to shape a prompt
and parse a response, exactly like `OllamaProvider`/`GroqProvider` in
legacy share one prompt/parsing implementation and differ only in
`_generate()`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.connection import DictConnection
from app.logging import get_logger
from app.services.llm import parsing, prompts
from app.services.llm.transport import Transport
from app.services.llm.types import Judgment, Summary

logger = get_logger(__name__)


def _current_base_system(conn: DictConnection | None) -> str:
    """`_build_system()` (llm.py:354-364): FIXED_GUIDE + admin-editable
    examples + admin-editable analyst guidance, re-read on every call
    (not cached) — matching legacy exactly, so an admin's mid-run edit
    to `relevance_guidance`/`relevance_examples` takes effect on the
    NEXT batch immediately, same as it always has. `conn=None` (no DB
    available, e.g. a unit test exercising `LLMClient` directly) falls
    back to `DEFAULT_EXAMPLES` + no guidance, which is what
    `get_relevance_examples`/`get_relevance_guidance` would themselves
    return against an empty `settings` table anyway.
    """
    if conn is None:
        return prompts.build_system_guide(prompts.DEFAULT_EXAMPLES, "")
    examples = prompts.get_relevance_examples(conn)
    guidance = prompts.get_relevance_guidance(conn)
    return prompts.build_system_guide(examples, guidance)


@dataclass
class LLMClient:
    """`transport` does the actual network call; `name` is a
    human-readable label for logs/admin UI (mirrors legacy's
    `provider.name`, e.g. `"groq · qwen/qwen3.6-27b"`); `conn` is
    optional and used only to read admin-editable guidance/examples —
    passing `None` is valid and exercises every method against
    `DEFAULT_EXAMPLES` with no extra guidance.
    """

    transport: Transport
    name: str
    conn: DictConnection | None = None

    # ── 1.1 judge() — DEAD IN PRODUCTION, documented per the "extract
    # everything verbatim" mandate (p3-contract.md §1.1, §7). ──────────

    def judge(self, title: str, source: str, sections: str) -> Judgment:
        system = prompts.judge_system(_current_base_system(self.conn))
        prompt = prompts.judge_prompt(title, source)
        try:
            raw = self.transport._generate(prompt, system=system, fmt="json")
        except Exception as e:  # noqa: BLE001 - mirrors llm.py's blanket except Exception
            return parsing.judge_whole_call_fallback(e)
        return parsing.parse_judge(raw)

    # ── 1.2 judge_batch() — the LIVE relevance-scoring call ────────────

    def judge_batch(self, items: list[dict]) -> list[Judgment]:
        if not items:
            return []
        system = prompts.judge_batch_system(_current_base_system(self.conn), len(items))
        prompt = prompts.judge_batch_prompt(items)
        try:
            raw = self.transport._generate(prompt, system=system, fmt="json")
        except Exception as e:  # noqa: BLE001
            return parsing.judge_batch_whole_call_fallback(list(range(len(items))), e)
        return parsing.parse_judge_batch(raw, len(items))

    # ── 1.3 cluster_topics_chunk() — topic labelling, Phase 1 ──────────

    def cluster_topics_chunk(self, items: list[dict], known_labels: list[str]) -> list[str]:
        if not items:
            return []
        system = prompts.cluster_topics_system(known_labels, len(items))
        prompt = prompts.cluster_topics_prompt(items)
        try:
            raw = self.transport._generate(
                prompt, system=system, fmt="json", num_predict=prompts.CLUSTER_TOPICS_NUM_PREDICT
            )
        except Exception:  # noqa: BLE001
            return parsing.cluster_topics_chunk_whole_call_fallback(list(range(len(items))))
        return parsing.parse_cluster_topics_chunk(raw, len(items))

    # ── 1.4 consolidate_labels() — topic-label merge ────────────────────

    def consolidate_labels(self, labels: list[str]) -> dict[str, str]:
        filtered = [label for label in labels if label]
        if len(filtered) < 2:
            return {label: label for label in filtered}
        system = prompts.consolidate_labels_system(len(filtered))
        prompt = prompts.consolidate_labels_prompt(filtered)
        try:
            raw = self.transport._generate(
                prompt, system=system, fmt="json", num_predict=prompts.CONSOLIDATE_LABELS_NUM_PREDICT
            )
        except Exception:  # noqa: BLE001
            return parsing.consolidate_labels_whole_call_fallback(filtered)
        return parsing.parse_consolidate_labels(raw, filtered)

    # ── 1.5 find_duplicates_batch() — topic-fold / standout decision ───

    def find_duplicates_batch(self, shown: list[dict], candidates: list[dict]) -> dict[int, int]:
        if not candidates:
            return {}
        shown_ref = shown[: prompts.FIND_DUPLICATES_SHOWN_CAP]
        system = prompts.find_duplicates_system()
        prompt = prompts.find_duplicates_prompt(shown, candidates)
        num_ctx = prompts.find_duplicates_num_ctx(system, prompt)
        try:
            raw = self.transport._generate(
                prompt,
                system=system,
                fmt="json",
                num_predict=prompts.FIND_DUPLICATES_NUM_PREDICT,
                num_ctx=num_ctx,
            )
        except Exception:  # noqa: BLE001
            return parsing.find_duplicates_batch_whole_call_fallback()
        return parsing.parse_find_duplicates_batch(raw, len(candidates), len(shown_ref))

    # ── 1.6 summarize() — DEAD (zero callers) ───────────────────────────

    def summarize(self, title: str, source: str, body: str = "") -> Summary:
        system = prompts.summarize_system()
        prompt = prompts.summarize_prompt(title, source, body)
        try:
            raw = self.transport._generate(prompt, system=system)
        except Exception:  # noqa: BLE001
            return Summary(text=parsing.summarize_whole_call_fallback())
        return Summary(text=parsing.parse_summarize(raw))

    # ── 1.7 translate_title() — headline translation ───────────────────

    def translate_title(self, title: str) -> str:
        system = prompts.translate_title_system()
        try:
            # p3-contract.md §1.7: the prompt IS the raw title, no template.
            raw = self.transport._generate(title, system=system)
        except Exception:  # noqa: BLE001
            return parsing.translate_title_whole_call_fallback()
        return parsing.parse_translate_title(raw)

    # ── 1.7b translate_titles_batch() — the BATCHED translate call ──────

    def translate_titles_batch(self, titles: list[str]) -> list[str]:
        """Translate many headlines in ONE request, returning one result
        per input title in INPUT ORDER.

        Positional alignment is a correctness requirement, not a
        convenience: `stage_translate` zips this back onto article rows,
        so a shifted result would write one article's headline onto
        another. `parsing.parse_translate_titles_batch` keys off the
        numbers in the response rather than trusting the model to
        preserve order, and defaults any missing index to that item's
        ORIGINAL title.

        On a whole-call failure every title falls back to itself, which
        is the same observable outcome as `translate_title`'s documented
        `""` fallback (the caller keeps the original) — see
        p3-contract.md §7.
        """
        if not titles:
            return []
        system = prompts.translate_titles_batch_system(len(titles))
        prompt = prompts.translate_titles_batch_prompt(titles)
        try:
            raw = self.transport._generate(prompt, system=system, fmt="json")
        except Exception as exc:  # noqa: BLE001 - mirrors judge_batch's blanket except
            # LOGGED, unlike the legacy blanket-except call sites. The
            # fallback here is indistinguishable from success at the
            # database level — every article simply keeps its original
            # title — so without this line a systemic failure (an expired
            # key, a dead model, a dropped DB connection breaking the
            # quota lookup) reads as "nothing needed translating" for an
            # entire run. That exact confusion cost real debugging time:
            # a closed connection surfaced as 25/25 titles "translated"
            # to themselves in 0.00s.
            logger.warning(
                "translate_batch_failed_keeping_originals",
                extra={"count": len(titles), "error_type": type(exc).__name__, "error": str(exc)[:200]},
            )
            return list(titles)
        return parsing.parse_translate_titles_batch(raw, titles)

    # ── 1.8 draft_bullet() — report-bullet drafting ─────────────────────

    def draft_bullet(self, title: str, body: str, section: str) -> str:
        system = prompts.draft_bullet_system()
        prompt = prompts.draft_bullet_prompt(title, body, section)
        try:
            raw = self.transport._generate(prompt, system=system)
        except Exception:  # noqa: BLE001
            return parsing.draft_bullet_whole_call_fallback(title)
        return parsing.parse_draft_bullet(raw)

    # ── 1.9 suggest_guidance() — human-in-the-loop, NEVER auto-applied ─
    # p3-contract.md §3: the ONE method with no try/except of its own —
    # an exception must propagate to the caller (legacy: main.py's own
    # try/except around the API call). Do not add one here.

    def suggest_guidance(self, approved: list[str], rejected: list[str]) -> str:
        system = prompts.suggest_guidance_system()
        prompt = prompts.suggest_guidance_prompt(approved, rejected)
        raw = self.transport._generate(prompt, system=system)
        return parsing.parse_suggest_guidance(raw)

    # ── 1.10 verify_group() — same-event verification ──────────────────

    def verify_group(self, headlines: list[str]) -> list[int]:
        if len(headlines) <= 1:
            return [0] * len(headlines)
        system = prompts.verify_group_system()
        prompt = prompts.verify_group_prompt(headlines)
        try:
            raw = self.transport._generate(prompt, system=system, fmt="json")
        except Exception:  # noqa: BLE001
            return parsing.verify_group_whole_call_fallback(len(headlines))
        return parsing.parse_verify_group(raw, len(headlines))

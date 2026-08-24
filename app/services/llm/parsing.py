"""Output parsing, clamping, and malformed-response fallback logic —
verbatim port of `backend/llm.py`'s per-call-site `try/except` blocks,
per p3-contract.md §3 (the frozen parsing/fallback table) and §10
(documented failure-granularity quirks, preserved on purpose, not
"fixed").

This is the REAL implementation `app.services.llm.provider` calls —
distinct from (but behaviorally identical to)
`tests/helpers/legacy_reference.py`'s reference-oracle port, which
exists only so the test harness had something to assert against before
this module existed (see docs/test-strategy.md §7, "Hooking in the real
rebuild": "P3-LLM: ... should be re-run (or duplicated) against
app/services/llm.py's real parsing functions"). `tests/test_p3_parsing.py`
duplicates `tests/test_parity_schema.py`'s assertions against THIS
module, fed the same `tests/helpers/fake_llm.py` failure shapes.

Every function below preserves the EXACT try/except granularity legacy
uses — some catch per-entry (one bad entry doesn't discard the whole
response), one (`parse_verify_group`) catches only via a length check
with NO per-element safety net (p3-contract.md §10.5) — because that
coarser granularity IS the frozen behavior, not a bug to quietly
improve.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.llm.types import Judgment

# ── Constants quoted from p3-contract.md §3's table cells ──────────────

JUDGE_SCORE_DEFAULT = 5  # data.get("score", 5) — key missing entirely
JUDGE_REASON_MAXLEN = 120
JUDGE_BATCH_MISSING_INDEX = (5, "no score returned")  # by_n.get(i+1, (5, "no score returned"))
CLUSTER_LABEL_MAXLEN = 60
CONSOLIDATE_LABEL_MAXLEN = 60
TRANSLATE_MAXLEN = 300
SUMMARIZE_MAXLEN = 400
DRAFT_BULLET_MAXLEN = 400
SUGGEST_GUIDANCE_MAXLEN = 600
TRANSLATE_STRIP_CHARS = "\"“”'"  # .strip('"“”\'')  (llm.py:657)
DRAFT_BULLET_STRIP_CHARS = "•-* "  # .lstrip("•-* ")  (llm.py:691)


# ── The one place this module deliberately BREAKS parity with legacy ───
#
# Everything else in this file is a faithful port whose quirks are
# frozen on purpose. `judge_batch` is not, any more, and the reason is
# that its frozen fallback manufactured a MEASUREMENT.
#
# Legacy's contract said: on any parse failure, hand every item back as
# `Judgment(relevant=True, score=5, reason="llm error: ...")`, and hand
# any item the model skipped back as `(5, "no score returned")`. Both
# paths RETURN NORMALLY. `app.services.enrichment`'s judge step cannot
# tell them from a real verdict, so it does
# `UPDATE articles SET llm_score=5` for the whole chunk, `StageCalls`
# records `ok=True`, and the run panel prints "done".
#
# 5 is not an inert placeholder in this product. It is the bottom of the
# GREEN "Low" band: the article shows up in the review queue as a scored
# chip, it counts green in `/dashboard/today`'s relevance mix, it lands
# in band `low` in `rollup_monthly_score_band`, and it moves
# `score_sum`/`scored_count` — i.e. the mean severity on the CRO's
# Economy and Telecom boards. A whole day's articles can be scored 5 by
# a proxy returning an HTML error page with HTTP 200, or by a model that
# wraps its JSON in a sentence of prose, and every surface in the
# product will report that day as normal.
#
# A TRANSPORT failure was always handled correctly — `LLMClient.judge_batch`
# re-raises it, `enrichment` leaves the batch unscored and records the
# failure. These two paths are the same failure arriving through a
# different door, so they now behave the same way:
#
#   * an unusable response body    -> raise `UnusableJudgeResponse`;
#     the batch is left unscored, exactly as a 500 would leave it.
#   * an item the model skipped    -> that ONE item comes back as
#     `NO_SCORE`, which `enrichment` writes nothing for.
#
# An unscored row is not lost: `llm_score IS NULL` keeps it pending and
# in the next run's selection (`_select_where`), so the cost of failing
# honestly is one retry, and the cost of the old behaviour was a
# fabricated number nobody could ever spot.
#
# The legacy helpers below (`judge_batch_whole_call_fallback`,
# `JUDGE_BATCH_MISSING_INDEX`) are kept, unused by the live path, purely
# so `tests/helpers/legacy_reference.py`'s oracle and the parity suite
# still have this module's copy of the behaviour they document.


class UnusableJudgeResponse(Exception):
    """The provider answered, and the answer cannot be read as scores.

    Raised instead of returning a value because there is no value that
    honestly represents "no scores" in a `list[Judgment]` — see the
    block comment above. `LLMClient.judge_batch` does not catch it (its
    own `try` covers only the transport call), so it propagates to
    `enrichment`'s judge step, which already knows how to leave a batch
    unscored and record the failure.
    """


# The score a `Judgment` carries when the model did not answer for that
# item at all. Out of band by construction: `clamp_score` can only ever
# produce 1-10, so 0 cannot collide with a real verdict, and the
# `articles.llm_score` column never sees it — `enrichment` skips the row.
NO_SCORE = 0
NO_SCORE_REASON = "no score returned for this item"


def no_score_judgment() -> Judgment:
    return Judgment(relevant=False, score=NO_SCORE, reason=NO_SCORE_REASON)


def is_unscored(judgment: Judgment) -> bool:
    """True for a `Judgment` the model never actually produced."""
    return judgment.score <= NO_SCORE


def clamp_score(raw_score: Any) -> int:
    """`max(1, min(10, int(round(float(data.get("score", 5))))))`
    (p3-contract.md §3, judge()/judge_batch() row).
    """
    return max(1, min(10, int(round(float(raw_score)))))


# ── Whole-call fallback constants (p3-contract.md §3's "fallback on ANY
# exception" column / §7's per-stage outcome table) ─────────────────────


def judge_whole_call_fallback(error: Exception) -> Judgment:
    """judge(): `Judgment(relevant=True, score=5, reason=f"llm error: {e}")`
    on ANY exception — the error message itself becomes the visible reason.
    """
    return Judgment(relevant=True, score=5, reason=f"llm error: {error}")


def judge_batch_whole_call_fallback(items: list[Any], error: Exception) -> list[Judgment]:
    """judge_batch(): whole-batch failure -> EVERY item gets the SAME
    neutral score=5 fallback with the SAME error string as its reason —
    not an empty list, not per-item errors.
    """
    reason = f"llm error: {error}"
    return [Judgment(relevant=True, score=5, reason=reason) for _ in items]


def cluster_topics_chunk_whole_call_fallback(items: list[Any]) -> list[str]:
    """cluster_topics_chunk(): whole-chunk failure -> `["" for _ in items]`
    — every item comes back untagged.
    """
    return ["" for _ in items]


def consolidate_labels_whole_call_fallback(labels: list[str]) -> dict[str, str]:
    """consolidate_labels(): whole-call failure -> identity mapping, no
    merging.
    """
    return {label: label for label in labels}


def find_duplicates_batch_whole_call_fallback() -> dict[int, int]:
    """find_duplicates_batch(): whole-batch failure -> `{}` — nothing
    folds, every candidate becomes its own standout card (the documented
    "safe" direction).
    """
    return {}


def verify_group_whole_call_fallback(headline_count: int) -> list[int]:
    """verify_group(): failure (including a single element failing
    `int()`, which discards the WHOLE response per p3-contract.md §10.5,
    not just that element) -> `list(range(len(headlines)))` — every
    headline becomes its own singleton group, the maximally conservative
    "never wrongly merge" fallback.
    """
    return list(range(headline_count))


def translate_title_whole_call_fallback() -> str:
    """translate_title(): failure -> `""`; caller (stage_translate) then
    falls back to the ORIGINAL untranslated title.
    """
    return ""


def summarize_whole_call_fallback() -> str:
    """summarize(): failure -> `Summary(text="")`. DEAD in production
    (zero callers) but the fallback contract is still part of the frozen
    spec.
    """
    return ""


def draft_bullet_whole_call_fallback(title: str) -> str:
    """draft_bullet(): failure -> returns the ORIGINAL `title` verbatim —
    the ONLY method in the whole file whose failure fallback is the
    *input*, not a blank/neutral value.
    """
    return title


# ── Full parse/clamp/fallback ports, raw-text-in -> parsed-value-out ───


def parse_judge(raw: str) -> Judgment:
    """Port of `judge()`'s post-`_generate()` logic (p3-contract.md
    §1.1). DEAD in production (judge_batch is the live call site) but
    implemented per the "extract everything verbatim" mandate.
    """
    try:
        data = json.loads(raw)
        score = clamp_score(data.get("score", JUDGE_SCORE_DEFAULT))
        return Judgment(relevant=(score >= 5), score=score, reason=str(data.get("reason", ""))[:JUDGE_REASON_MAXLEN])
    except Exception as e:  # noqa: BLE001 - mirrors llm.py's blanket except Exception
        return judge_whole_call_fallback(e)


def parse_judge_batch(raw: str, item_count: int) -> list[Judgment]:
    """Parse `judge_batch()`'s JSON into one `Judgment` per input item,
    IN INPUT ORDER (p3-contract.md §1.2 for the wire shape).

    DELIBERATELY NOT the frozen legacy fallback behaviour — see the
    "the one place this module deliberately BREAKS parity" block near
    the top of this file for the full reasoning and what the old
    behaviour cost.

    Raises `UnusableJudgeResponse` if the body cannot be read as scores
    at all (not JSON, not an object, an entry whose `n`/`score` is not a
    number). Items the model simply omitted come back as `NO_SCORE`
    judgments — the batch still yields the verdicts it did return, and
    the caller writes nothing for the rest.
    """
    try:
        data = json.loads(raw)
        by_n: dict[int, tuple[int, str]] = {}
        for entry in data.get("scores", []):
            n = int(entry.get("n", 0))
            sc = clamp_score(entry.get("score", JUDGE_SCORE_DEFAULT))
            by_n[n] = (sc, str(entry.get("reason", ""))[:JUDGE_REASON_MAXLEN])
    except Exception as exc:  # noqa: BLE001 - every parse failure is one outcome: unusable
        raise UnusableJudgeResponse(
            f"could not read scores from a {len(raw)}-char response: {type(exc).__name__}: {exc}"
        ) from exc

    out = []
    for i in range(item_count):
        scored = by_n.get(i + 1)
        if scored is None:
            out.append(no_score_judgment())
            continue
        sc, reason = scored
        out.append(Judgment(relevant=(sc >= 5), score=sc, reason=reason))
    # A response that answered for NOTHING is an unusable response, not
    # a batch of misses: the realistic cause is a body that parsed as
    # JSON but is not this call's answer at all (a proxy's `{"error":
    # ...}` page, or a model that replied with an empty envelope), and
    # calling that "the model skipped all ten" would understate it in
    # the run panel.
    if item_count and not by_n:
        raise UnusableJudgeResponse(
            f"response parsed as JSON but carried no usable score entries for {item_count} item(s)"
        )
    return out


def count_unscored(judgments: list[Judgment]) -> int:
    """How many items in a parsed batch the model never answered for."""
    return sum(1 for j in judgments if is_unscored(j))


def parse_translate_titles_batch(raw: str, titles: list[str]) -> list[str]:
    """Parse `translate_titles_batch`'s JSON into one translation per
    input title, IN INPUT ORDER.

    Failure grain matches `parse_judge_batch` deliberately: a malformed
    whole response falls back to the ORIGINAL titles (which is exactly
    what `stage_translate`'s single-call path does when a call fails —
    p3-contract.md §7's "never silently drop"), and a merely missing or
    mis-numbered index falls back per item, not for the whole batch.

    Returning the original title rather than "" matters: `stage_translate`
    treats an empty string as "translation failed, keep the original",
    so both paths converge on the same observable outcome, but doing it
    here keeps the positional alignment explicit and auditable.
    """
    try:
        data = json.loads(raw)
        by_n: dict[int, str] = {}
        for entry in data.get("translations", []):
            n = int(entry.get("n", 0))
            text = str(entry.get("text", "")).strip()
            if text:
                by_n[n] = text[:TRANSLATE_MAXLEN]
        return [by_n.get(i + 1, titles[i]) for i in range(len(titles))]
    except Exception:  # noqa: BLE001 - mirrors parse_judge_batch's blanket except
        return list(titles)


def parse_cluster_topics_chunk(raw: str, item_count: int) -> list[str]:
    """Port of `cluster_topics_chunk()`'s post-`_generate()` logic
    (p3-contract.md §1.3). Per-ENTRY `try/except (ValueError, TypeError):
    continue` on the index — one bad entry doesn't discard the response —
    but the whole call still falls back to `["" for _ in items]` on a
    total parse failure (bad top-level JSON, etc).
    """
    try:
        data = json.loads(raw)
        by_n: dict[int, str] = {}
        for entry in data.get("labels", []):
            try:
                n = int(entry.get("n", 0))
            except (ValueError, TypeError):
                continue
            label = str(entry.get("label", "")).strip()[:CLUSTER_LABEL_MAXLEN]
            if label:
                by_n[n] = label
        return [by_n.get(i + 1, "") for i in range(item_count)]
    except Exception:  # noqa: BLE001
        return cluster_topics_chunk_whole_call_fallback(list(range(item_count)))


def parse_consolidate_labels(raw: str, labels: list[str]) -> dict[str, str]:
    """Port of `consolidate_labels()`'s post-`_generate()` logic
    (p3-contract.md §1.4). Every input label not returned by the model
    maps to itself (`setdefault`); whole-call failure -> full identity
    map.
    """
    try:
        data = json.loads(raw)
        out: dict[str, str] = {}
        for entry in data.get("map", []) if isinstance(data, dict) else []:
            try:
                idx = int(entry.get("n", 0)) - 1
            except (ValueError, TypeError, AttributeError):
                continue
            canon = str(entry.get("canon", "")).strip()[:CONSOLIDATE_LABEL_MAXLEN]
            if 0 <= idx < len(labels) and canon:
                out[labels[idx]] = canon
        for label in labels:
            out.setdefault(label, label)
        return out
    except Exception:  # noqa: BLE001
        return consolidate_labels_whole_call_fallback(labels)


def parse_find_duplicates_batch(raw: str, candidate_count: int, shown_count: int) -> dict[int, int]:
    """Port of `find_duplicates_batch()`'s post-`_generate()` logic
    (p3-contract.md §1.5). Per-entry try/except plus bounds-checking
    against BOTH candidate_count and shown_count; a candidate absent
    from the result is "not a duplicate", not an error.
    """
    try:
        data = json.loads(raw)
        out: dict[int, int] = {}
        for d in data.get("dupes", []) if isinstance(data, dict) else []:
            try:
                ci = int(d.get("c", 0)) - 1
                si = int(d.get("same_as", 0)) - 1
            except (ValueError, TypeError, AttributeError):
                continue
            if 0 <= ci < candidate_count and 0 <= si < shown_count:
                out[ci] = si
        return out
    except Exception:  # noqa: BLE001
        return find_duplicates_batch_whole_call_fallback()


def parse_verify_group(raw: str, headline_count: int) -> list[int]:
    """Port of `verify_group()`'s post-`_generate()` logic
    (p3-contract.md §1.10). ONLY a length check, no per-element
    try/except before the list comprehension — p3-contract.md §10.5's
    documented coarser failure granularity: if `int(g)` fails on ANY
    single element, the exception escapes the list comprehension and
    the WHOLE response is discarded, even though every other element
    parsed fine.
    """
    try:
        groups = json.loads(raw).get("groups", [])
        if len(groups) == headline_count:
            return [int(g) for g in groups]
    except Exception:  # noqa: BLE001
        pass
    return verify_group_whole_call_fallback(headline_count)


def parse_translate_title(raw: str) -> str:
    """Port of `translate_title()`'s post-`_generate()` logic
    (p3-contract.md §1.7): `.strip()` then strip leading/trailing quote
    characters, truncated to 300 chars. Any exception (including one
    raised by the caller's own `_generate()` before this function is
    even reached) -> `""`; that has to be handled by the CALLER wrapping
    `_generate()`, since this function only receives text that already
    exists, mirroring the split between `_generate()`'s own
    `try/except` scope and this parse step.
    """
    text = raw.strip()
    return text.strip(TRANSLATE_STRIP_CHARS)[:TRANSLATE_MAXLEN]


def parse_draft_bullet(raw: str) -> str:
    """Port of `draft_bullet()`'s post-`_generate()` logic: `.strip()`,
    then `.lstrip("•-* ")` (strips any leading bullet char, hyphen,
    asterisk, or space the model added), truncated to 400 chars.
    """
    text = raw.strip()
    return text.lstrip(DRAFT_BULLET_STRIP_CHARS).strip()[:DRAFT_BULLET_MAXLEN]


def parse_summarize(raw: str) -> str:
    """Port of `summarize()`'s post-`_generate()` logic: truncate to 400
    chars, no other transform.
    """
    return raw[:SUMMARIZE_MAXLEN]


def parse_suggest_guidance(raw: str) -> str:
    """Port of `suggest_guidance()`'s post-`_generate()` logic: truncate
    to 600 chars. NOTE (p3-contract.md §3): this is the one method with
    no `try/except` of its own in legacy — an exception from
    `_generate()` propagates to the caller (`main.py`'s own
    try/except). This function only handles the truncation; the
    provider method must NOT wrap its `_generate()` call in a
    try/except, to preserve that exact propagation behavior.
    """
    return raw[:SUGGEST_GUIDANCE_MAXLEN]


# ── Batching contract constants (p3-contract.md §5) — exact-match ──────
# Documented DEFAULT env-var-driven batch sizes. A rebuild is free to
# make them configurable (app.services.llm.enrichment reads them from
# routing/config, not these constants, at call time) — but the DEFAULTS
# below must match, since §5's ordering/mapping guarantees were derived
# from these exact numbers.

# Single-call translation remains the frozen legacy contract
# (p3-contract.md §1.7) and is still the fallback path.
BATCH_SIZE_TRANSLATE = 1
# Headlines per BATCHED translate call (`translate_titles_batch`). 25 is
# sized against the binding constraint, which is tokens-per-minute, not
# requests: a Bengali headline is ~40-60 tokens, so 25 in + 25 out lands
# around 2.5-3k tokens per call -- comfortably inside Mistral's measured
# 50,000 TPM while cutting a 1,376-article day from 1,376 calls to ~55.
BATCH_SIZE_TRANSLATE_BATCHED = 25
BATCH_SIZE_DEDUP_VERIFY = 12  # hard-coded CHUNK, pipeline.py:189
BATCH_SIZE_JUDGE_DEFAULT = 10  # LLM_BATCH env default, collection_service.py:383
BATCH_SIZE_TOPIC_CHUNK_DEFAULT = 50  # LLM_TOPIC_CHUNK env default, :429
BATCH_SIZE_TOPIC_DEDUP_DEFAULT = 8  # LLM_TOPIC_DEDUP_BATCH env default, :515
TOPIC_DEDUP_SHOWN_CAP = 30  # hard-coded shown[:30], llm.py:572

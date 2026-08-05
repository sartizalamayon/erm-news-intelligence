"""Reference-oracle ports of the legacy app's DETERMINISTIC, non-LLM logic.

Every function here is a verbatim (or near-verbatim, comment-annotated
where it differs) port of code quoted in `docs/legacy-behavior.md` or
`docs/p3-contract.md`, cited by file:line at each function. This module
exists so the test harness has a REAL, callable "expected behavior"
oracle for logic that is 100% deterministic and therefore testable
without any model call — P2 keyword matching (legacy-behavior.md §4) and
the parsing/clamping/fallback logic P3's JSON-mode calls apply to
whatever text a model returns (p3-contract.md §3).

IMPORTANT — this is NOT the rebuild's implementation. P1-FETCH / P2-MATCH
/ P3-LLM must still write `app/services/...` from scratch; nothing here
is imported by `app/`. This module's job is narrower: give
`tests/test_p1p2_equivalence.py` and `tests/test_parity_schema.py`
something concrete to assert against today, in place of code that
doesn't exist yet, so those tests exercise a REAL algorithm (verified
against real historical fixture output, not invented). Once the rebuild
lands, its own equivalent functions should be swapped in as the
comparison target — see docs/test-strategy.md "Hooking in the real
rebuild" for exactly where.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# ── P2 — keyword matching (legacy-behavior.md §4.1-4.3) ────────────────


def is_bengali(s: str) -> bool:
    """collector_logic._is_bengali() (collector_logic.py:487-489)."""
    return any("ঀ" <= ch <= "৿" for ch in s)


@dataclass(frozen=True)
class CompiledPattern:
    section: str
    label: str
    pattern: re.Pattern[str]


def build_patterns(groups: dict[str, list[tuple[str, str]]]) -> list[CompiledPattern]:
    """collector_logic.build_patterns() (collector_logic.py:491-506), verbatim
    logic: Bengali terms compile to a plain re.escape() substring match (no
    word boundaries — Bengali script has no ASCII \\b semantics, no
    case-folding); English/ASCII terms compile to \\bterm\\b, IGNORECASE.
    """
    patterns: list[CompiledPattern] = []
    for section, terms in groups.items():
        for term, label in terms:
            if not term.strip():
                continue
            if is_bengali(term):
                pat = re.compile(re.escape(term))
            else:
                pat = re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
            patterns.append(CompiledPattern(section, label, pat))
    return patterns


def patterns_from_keyword_rows(rows: list[dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    """collection_service._patterns_from_db() (collection_service.py:46-69),
    the DB-shape -> build_patterns()-input adapter: the English term is
    added once with itself as its own label; each comma-split Bengali
    synonym is added as its own (bn_term, label=english_term) pair, so a
    Bengali-only match is still reported under its English label.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        section = row["section"]
        term_en = (row.get("term_en") or "").strip()
        term_bn = row.get("term_bn") or ""
        pairs = groups.setdefault(section, [])
        if term_en:
            pairs.append((term_en, term_en))
        for bn_term in term_bn.split(","):
            bn_term = bn_term.strip()
            if bn_term and term_en:
                pairs.append((bn_term, term_en))
    return groups


def match_keywords(text: str, patterns: list[CompiledPattern]) -> tuple[str, str]:
    """collector_logic.match_keywords() (collector_logic.py:508-515), verbatim:
    runs EVERY pattern against `text` (no short-circuit), accumulates into
    sets (so English+Bengali synonyms of the same label collapse to one
    entry), returns two alphabetically-sorted, comma-joined strings. Empty
    string in both positions = no match.
    """
    secs: set[str] = set()
    kws: set[str] = set()
    for cp in patterns:
        if cp.pattern.search(text):
            secs.add(cp.section)
            kws.add(cp.label)
    return ", ".join(sorted(secs)), ", ".join(sorted(kws))


def review_status_for(sections: str) -> str:
    """collection_service.run_collection() Phase 2 step 7
    (collection_service.py, quoted in legacy-behavior.md §2.6 point 7):
    review_status = 'pending' if sections else 'filtered_out'.
    """
    return "pending" if sections else "filtered_out"


def match_type_for(sections: str) -> str:
    """Same call site: match_type = 'keyword' if sections else ''."""
    return "keyword" if sections else ""


# ── P3 — output parsing / clamping / fallback (p3-contract.md §3) ──────
#
# These are the deterministic, testable-without-a-model pieces of the
# JSON-mode call sites: given a SYNTHETIC raw model-response string (or
# its absence, i.e. a raised exception), what does the parsing/clamping
# logic do, and what is the EXACT fallback value on total failure. Every
# constant below is quoted from the p3-contract.md §3 table cell it
# reproduces.

JUDGE_SCORE_DEFAULT = 5  # data.get("score", 5) — key missing entirely
JUDGE_REASON_MAXLEN = 120
JUDGE_BATCH_MISSING_INDEX = (5, "no score returned")  # by_n.get(i+1, (5, "no score returned"))
CLUSTER_LABEL_MAXLEN = 60
CONSOLIDATE_LABEL_MAXLEN = 60
TRANSLATE_MAXLEN = 300
SUMMARIZE_MAXLEN = 400
DRAFT_BULLET_MAXLEN = 400
SUGGEST_GUIDANCE_MAXLEN = 600
TRANSLATE_STRIP_CHARS = '"“”\''  # .strip('"“”\'')
DRAFT_BULLET_STRIP_CHARS = "•-* "  # .lstrip("•-* ")


def clamp_score(raw_score: Any) -> int:
    """`max(1, min(10, int(round(float(data.get("score", 5))))))`
    (p3-contract.md §3, judge()/judge_batch() row). Shared clamp/round
    logic for both single-item and per-batch-entry scores.
    """
    return max(1, min(10, int(round(float(raw_score)))))


@dataclass(frozen=True)
class JudgeFallback:
    relevant: bool
    score: int
    reason_prefix: str


def judge_whole_call_fallback(error: Exception) -> JudgeFallback:
    """judge(): `Judgment(relevant=True, score=5, reason=f"llm error: {e}")`
    on ANY exception (p3-contract.md §3 row 1) — the error message itself
    becomes the visible reason.
    """
    return JudgeFallback(relevant=True, score=5, reason_prefix=f"llm error: {error}")


def judge_batch_whole_call_fallback(items: list[Any], error: Exception) -> list[JudgeFallback]:
    """judge_batch(): whole-batch failure -> EVERY item gets the SAME
    neutral score=5 fallback with the SAME error string as its reason
    (p3-contract.md §3 row 2) — not an empty list, not per-item errors.
    """
    reason = f"llm error: {error}"
    return [JudgeFallback(relevant=True, score=5, reason_prefix=reason) for _ in items]


def cluster_topics_chunk_whole_call_fallback(items: list[Any]) -> list[str]:
    """cluster_topics_chunk(): whole-chunk failure -> `["" for _ in items]`
    (p3-contract.md §3 row 3) — every item comes back untagged.
    """
    return ["" for _ in items]


def consolidate_labels_whole_call_fallback(labels: list[str]) -> dict[str, str]:
    """consolidate_labels(): whole-call failure -> identity mapping, no
    merging (p3-contract.md §3 row 4).
    """
    return {label: label for label in labels}


def find_duplicates_batch_whole_call_fallback() -> dict[int, int]:
    """find_duplicates_batch(): whole-batch failure -> `{}` — nothing
    folds, every candidate becomes its own standout card (p3-contract.md
    §3 row 5, documented as the "safe" direction).
    """
    return {}


def verify_group_whole_call_fallback(headline_count: int) -> list[int]:
    """verify_group(): failure (including a single element failing
    `int()`, which discards the WHOLE response per p3-contract.md §10.5,
    not just that element) -> `list(range(len(headlines)))` — every
    headline becomes its own singleton group, the maximally conservative
    "never wrongly merge" fallback (p3-contract.md §3 row 6).
    """
    return list(range(headline_count))


def translate_title_whole_call_fallback() -> str:
    """translate_title(): failure -> `""`; caller (stage_translate) then
    falls back to the ORIGINAL untranslated title (p3-contract.md §3 row 7
    + §7 table).
    """
    return ""


def summarize_whole_call_fallback() -> str:
    """summarize(): failure -> `Summary(text="")` (p3-contract.md §3 row 8).
    DEAD in production (zero callers) but the fallback contract is still
    part of the frozen spec — see p3-contract.md §1.6.
    """
    return ""


def draft_bullet_whole_call_fallback(title: str) -> str:
    """draft_bullet(): failure -> returns the ORIGINAL `title` verbatim —
    the ONLY method in the whole file whose failure fallback is the
    *input*, not a blank/neutral value (p3-contract.md §3 row 9, §10.7).
    """
    return title


# ── Full parse/clamp/fallback ports, raw-text-in -> parsed-value-out ───
#
# These take the exact string a `_generate()` call would have returned
# (successful, malformed, or truncated — see tests/helpers/fake_llm.py)
# and reproduce the FULL parsing control flow from p3-contract.md §1/§3,
# including the internal try/except structure (whole-call vs per-entry),
# not just the end-state fallback constant. This is what lets
# tests/test_parity_schema.py assert "given THIS raw text, the parsed
# result is EXACTLY this" end to end.


def parse_judge_batch(raw: str, item_count: int) -> list[JudgeFallback]:
    """Port of judge_batch()'s post-`_generate()` logic (p3-contract.md
    §1.2). Any exception during parsing (bad JSON, wrong shape) -> the
    WHOLE batch falls back to neutral score 5 with the exception string as
    every item's reason. On success, missing/mis-numbered indices default
    PER-ITEM to (5, "no score returned") — a much finer failure grain than
    the whole-call fallback.
    """
    try:
        data = json.loads(raw)
        by_n: dict[int, tuple[int, str]] = {}
        for entry in data.get("scores", []):
            n = int(entry.get("n", 0))
            sc = clamp_score(entry.get("score", JUDGE_SCORE_DEFAULT))
            by_n[n] = (sc, str(entry.get("reason", ""))[:JUDGE_REASON_MAXLEN])
        out = []
        for i in range(item_count):
            sc, reason = by_n.get(i + 1, JUDGE_BATCH_MISSING_INDEX)
            out.append(JudgeFallback(relevant=(sc >= 5), score=sc, reason_prefix=reason))
        return out
    except Exception as e:  # noqa: BLE001 - mirrors llm.py's blanket except Exception
        return judge_batch_whole_call_fallback(list(range(item_count)), e)


def parse_cluster_topics_chunk(raw: str, item_count: int) -> list[str]:
    """Port of cluster_topics_chunk()'s post-`_generate()` logic
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
    """Port of consolidate_labels()'s post-`_generate()` logic
    (p3-contract.md §1.4). Every input label not returned by the model
    maps to itself (`setdefault`); whole-call failure -> full identity map.
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
    """Port of find_duplicates_batch()'s post-`_generate()` logic
    (p3-contract.md §1.5). Per-entry try/except plus bounds-checking
    against BOTH candidate_count and shown_count; a candidate absent from
    the result is "not a duplicate", not an error.
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
    """Port of verify_group()'s post-`_generate()` logic (p3-contract.md
    §1.10). ONLY a length check, no per-element try/except before the
    list comprehension — p3-contract.md §10.5's documented coarser
    failure granularity: if `int(g)` fails on ANY single element, the
    exception escapes the list comprehension and the WHOLE response is
    discarded, even though every other element parsed fine.
    """
    try:
        groups = json.loads(raw).get("groups", [])
        if len(groups) == headline_count:
            return [int(g) for g in groups]
    except Exception:  # noqa: BLE001
        pass
    return verify_group_whole_call_fallback(headline_count)


# ── Batching contract constants (p3-contract.md §5) — exact-match ──────
# These are the documented DEFAULT env-var-driven batch sizes. A rebuild
# is free to make them configurable, but the DEFAULTS must match, since
# §5's ordering/mapping guarantees were derived from these exact numbers.

BATCH_SIZE_TRANSLATE = 1  # no batching — one call per non-English title
BATCH_SIZE_DEDUP_VERIFY = 12  # hard-coded CHUNK, pipeline.py:189
BATCH_SIZE_JUDGE_DEFAULT = 10  # LLM_BATCH env default, collection_service.py:383
BATCH_SIZE_TOPIC_CHUNK_DEFAULT = 50  # LLM_TOPIC_CHUNK env default, :429
BATCH_SIZE_TOPIC_DEDUP_DEFAULT = 8  # LLM_TOPIC_DEDUP_BATCH env default, :515
TOPIC_DEDUP_SHOWN_CAP = 30  # hard-coded shown[:30], llm.py:572

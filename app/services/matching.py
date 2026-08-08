"""P2 — keyword matching (docs/legacy-behavior.md §4).

A verbatim-behavior port of `collector_logic.build_patterns()` /
`match_keywords()` / `_is_bengali()` (`collector_logic.py:487-515`) plus
the DB-shape adapter `collection_service._patterns_from_db()`
(`collection_service.py:46-69`). Internals here are free to differ from
legacy (mission: "P2 may be rewritten freely — only the OUTPUT must stay
equivalent") — this module keeps the internals close to legacy anyway
because the matching logic itself (word-boundary vs. substring, set
accumulation, sorted-comma-join) has no room for a "better" rewrite that
still satisfies GATE 3's empty-diff requirement. What *is* new here
relative to legacy: `load_patterns()` composes the DB read + grouping +
compilation into one call so a caller doesn't have to know the shape in
between (legacy's three responsibilities were split across
`collection_service._patterns_from_db()`,
`collector_logic.build_patterns()`, and the SQL itself); `build_patterns()`
is memoized (see `_build_patterns_cached` below) so a process that ends up
calling `load_patterns()` more than once in its lifetime doesn't
recompile the same ~580 regexes for free — legacy recompiles
unconditionally on every `run_collection()` call.

── Semantic search: NOT present here, by design ───────────────────────

legacy-behavior.md §5 lists six call sites for legacy's semantic-recall
second pass. None of them are ported to this module, and none of them
have an equivalent anywhere in this file:

  1. `semantic.py` (`build_reference_set`, `run_semantic_filter`) — the
     module itself. Not imported, not reimplemented. This matching
     engine has no embedding model, no reference-set construction, no
     cosine-similarity step.
  2. `collection_service._run_semantic_stage()` — the orchestration
     wrapper that stamped `match_type='keyword'`, pulled
     `filtered_out` rows, and called into `semantic.py`. No equivalent
     function exists here; `match_article()`/`match_articles()` below
     are the entire P2 surface, and neither promotes a
     keyword-non-match into anything.
  3. `collection_service.run_llm_enrichment()` STEP 0.6 (the
     `do_semantic` block) — a P3-stage concern, not P2, and doubly out
     of scope for this module regardless.
  4. `main.py`'s `/api/llm/start?stage=semantic` — a P3 API surface,
     not owned by this file.
  5. `articles.match_type` / `articles.semantic_score` columns — this
     module only ever writes `match_type` as `'keyword'` or `''`
     (`match_type_for()` below); it never writes `'semantic'` and never
     touches a `semantic_score` field at all.
  6. The review-UI semantic filter/badge (`main.py:187-192, 339-346,
     548, 583, 1007`) — a frontend concern, not this module's.

What is deliberately NOT removed: `dedup.py`'s shared embedding-model
loader (`_get_model()`/`_prep()`) backs P3 topic clustering
(`_embed_prefold()`), a different feature entirely (legacy-behavior.md
§5.7). This module doesn't import or depend on it either way — P2 has no
embedding step, so there is nothing here that could accidentally break
that dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app.db.connection import DictConnection

# ── Bengali detection (collector_logic._is_bengali, collector_logic.py:487-489) ──
# Bengali Unicode block is U+0980-U+09FF ("ঀ" - "৿"). Any character in that
# range is enough to call the whole string Bengali-scripted — legacy does
# not require every character to be Bengali, just at least one.
_BENGALI_LOW = "ঀ"
_BENGALI_HIGH = "৿"


def is_bengali(s: str) -> bool:
    """True if `s` contains at least one Bengali-script character."""
    return any(_BENGALI_LOW <= ch <= _BENGALI_HIGH for ch in s)


@dataclass(frozen=True)
class CompiledPattern:
    """One compiled regex plus the (section, label) it reports when it
    hits. `build_patterns()` produces one of these PER TERM, not per
    keyword row — a keyword row with 3 Bengali synonyms yields 4 entries
    (1 English + 3 Bengali), matching legacy exactly.
    """

    section: str
    label: str
    pattern: re.Pattern[str]


# {section: [(term, label), ...]} — the shape collection_service._patterns_from_db()
# builds before handing off to collector_logic.build_patterns().
KeywordGroups = dict[str, list[tuple[str, str]]]


def load_keywords(conn: DictConnection) -> list[dict[str, Any]]:
    """Read enabled keyword rows from the `keywords` table
    (migrations/0002_reference_tables.sql), the DB-read half of legacy's
    `collection_service._patterns_from_db()` (`:46-69`). 271 rows across
    5 sections in the current live data (legacy-behavior.md §4.1).

    KEYWORDS-CONFIG (migrations/0012_keywords_section_nullable.sql):
    `section` can now be SQL NULL ("no section assigned"). COALESCE'd to
    the literal string `"Uncategorized"` right here, at the read
    boundary, rather than downstream in `keyword_rows_to_groups` /
    `match_keywords` — those two build/consume a `{section: [...]}`
    dict and a `set()` of section labels respectively; either one
    seeing a bare `None` next to real section strings would raise on
    `match_keywords`'s `sorted(secs)` (`None < str` is unorderable in
    Python 3). Coercing at the SQL layer keeps every function below
    this one exactly as it was — none of them need to know NULL
    sections exist at all.
    """
    rows = conn.execute(
        "SELECT COALESCE(section, 'Uncategorized') AS section, term_en, term_bn FROM keywords WHERE enabled"
    ).fetchall()
    return [dict(row) for row in rows]


def keyword_rows_to_groups(rows: list[dict[str, Any]]) -> KeywordGroups:
    """Group DB keyword rows into `{section: [(term, label), ...]}` —
    the grouping half of `_patterns_from_db()`. The English term is
    added once with itself as its own label; each comma-split Bengali
    synonym is added as its own `(bn_term, label)` pair, so a
    Bengali-only match is still reported under one consistent label.

    KEYWORDS-CONFIG (bilingual entry, independently optional): `term_en`
    and `term_bn` can each be blank on their own now (never both — the
    API layer's `create_keyword` rejects that). Previously this function
    required a truthy `term_en` before adding ANY Bengali synonym
    (`if bn_term and term_en`), so a Bengali-only row silently compiled
    to zero patterns — a keyword an admin believed was live would never
    match anything. Fixed by computing one label per row up front
    (`term_en` if present, else that row's first Bengali synonym) and no
    longer gating the Bengali loop on `term_en` at all. Behavior is
    unchanged for every row that already had a `term_en` — same pairs,
    same order, same label.
    """
    groups: KeywordGroups = {}
    for row in rows:
        section = row["section"]
        pairs = groups.setdefault(section, [])
        term_en = (row.get("term_en") or "").strip()
        bn_terms = [t.strip() for t in (row.get("term_bn") or "").split(",") if t.strip()]
        label = term_en or (bn_terms[0] if bn_terms else "")
        if not label:
            continue
        if term_en:
            pairs.append((term_en, label))
        for bn_term in bn_terms:
            pairs.append((bn_term, label))
    return groups


def _groups_cache_key(groups: KeywordGroups) -> tuple[tuple[str, tuple[tuple[str, str], ...]], ...]:
    """A hashable, order-independent-per-section snapshot of `groups`,
    used only as an `lru_cache` key for `_build_patterns_cached`. Section
    order and within-section term order ARE preserved (matching output
    is order-independent per `match_keywords()`'s set accumulation, but
    keeping insertion order here costs nothing and avoids ever masking a
    genuine content change as a cache hit).
    """
    return tuple((section, tuple(terms)) for section, terms in groups.items())


@lru_cache(maxsize=8)
def _build_patterns_cached(
    key: tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
) -> tuple[CompiledPattern, ...]:
    patterns: list[CompiledPattern] = []
    for section, terms in key:
        for term, label in terms:
            if not term.strip():
                continue
            pattern = re.compile(re.escape(term)) if is_bengali(term) else re.compile(
                r"\b" + re.escape(term) + r"\b", re.IGNORECASE
            )
            patterns.append(CompiledPattern(section, label, pattern))
    return tuple(patterns)


def build_patterns(groups: KeywordGroups) -> list[CompiledPattern]:
    """Compile one regex per term (`collector_logic.build_patterns()`,
    `collector_logic.py:491-506`, verbatim logic): Bengali terms compile
    to a plain `re.escape()` substring match (no word boundaries —
    Bengali script has no ASCII `\\b` semantics, no case-folding);
    English/ASCII terms compile to `\\bterm\\b`, case-insensitive.

    Memoized (`_build_patterns_cached`) on the *content* of `groups`, not
    identity — calling this twice with equal-but-different dict/list
    objects (e.g. two `load_patterns()` calls in one long-lived process
    against an unchanged `keywords` table) skips recompiling the ~580
    regexes the second time. Legacy recompiles unconditionally on every
    `run_collection()` call; this is a pure micro-optimization with no
    output difference — see module docstring.
    """
    return list(_build_patterns_cached(_groups_cache_key(groups)))


def load_patterns(conn: DictConnection) -> list[CompiledPattern]:
    """One-shot convenience: read enabled keywords, group them, compile
    patterns. What a caller (P1-INGEST's collector) should call ONCE per
    run, exactly like legacy calls `_patterns_from_db()` once at the top
    of `run_collection()` (`collection_service.py:85`), then reuse the
    result for every article's `match_keywords()` call.
    """
    return build_patterns(keyword_rows_to_groups(load_keywords(conn)))


def match_keywords(text: str, patterns: list[CompiledPattern]) -> tuple[str, str]:
    """`collector_logic.match_keywords()` (`collector_logic.py:508-515`),
    verbatim: runs EVERY pattern against `text` (no short-circuit),
    accumulates into sets (so a title matching the same label via both
    its English and a Bengali synonym still reports the label once),
    returns two alphabetically-sorted, comma-joined strings. Empty
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
    """`review_status = 'pending' if sections else 'filtered_out'`
    (legacy-behavior.md §2.6 point 7 / `collection_service.py:161`).
    """
    return "pending" if sections else "filtered_out"


def match_type_for(sections: str) -> str:
    """`match_type = 'keyword' if sections else ''` — same call site.
    Never returns `'semantic'` or `'both'`: this module has no semantic
    stage (see module docstring) and legacy itself never writes `'both'`
    anywhere (legacy-behavior.md §5.5/§7 item 8, dead value).
    """
    return "keyword" if sections else ""


def match_article(article: dict[str, Any], patterns: list[CompiledPattern]) -> dict[str, Any]:
    """Given one P1 article record (must have a `title` key), return a
    NEW dict with P2's output columns layered on: `sections`, `keywords`,
    `review_status`, `match_type` (legacy-behavior.md §2.6 points 5 & 7).
    Does not mutate `article`.
    """
    sections, keywords = match_keywords(article["title"], patterns)
    return {
        **article,
        "sections": sections,
        "keywords": keywords,
        "review_status": review_status_for(sections),
        "match_type": match_type_for(sections),
    }


def match_articles(articles: list[dict[str, Any]], patterns: list[CompiledPattern]) -> list[dict[str, Any]]:
    """Batch form of `match_article()` — the whole P2 pass over one run's
    fetched articles, in insertion order (order doesn't affect any
    individual result; `match_keywords()` is set-based).
    """
    return [match_article(a, patterns) for a in articles]

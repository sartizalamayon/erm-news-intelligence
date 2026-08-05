"""Fixture generator — turns the live legacy corpus into the three frozen,
committed fixture tiers this repo's test suite runs against.

    python -m tests.fixtures.build_fixtures

WHY THIS SCRIPT EXISTS (see docs/test-strategy.md for the full rationale):
P1/P2/P3 of the legacy app already ran, for real, over real fetched
articles — `data/news.db` is not synthetic data, it is the recorded
OUTPUT of thousands of real pipeline runs. Rather than re-fetching the
internet or inventing synthetic inputs to test the rebuild against
(forbidden by mission §7), this script extracts real historical rows for
each pipeline stage's documented OUTPUT CONTRACT and freezes them as
JSON. Downstream tests read the frozen JSON only — never the live DB —
so CI (which never has `data/news.db`, it's gitignored) can still run
every fixture-consuming test.

READ-ONLY. Opens `data/news.db` with `mode=ro`; never writes to it.
This script is NOT invoked by pytest and NOT invoked by CI — it's a
manual, on-demand regeneration tool for a developer who has a local
copy of the corpus and wants to refresh the frozen fixtures (e.g. after
the corpus grows, or to pull a different sample window). Running it
overwrites the JSON files under `fixtures/` at the repo root; the
regenerated files should be reviewed like any other change before commit
(the sample is randomized-but-seeded, so a re-run with the same corpus
and the same SEED reproduces byte-identical output — see `SEED` below).

Three tiers produced, named per the mission brief
(`fixtures/{tier}_{YYYYMMDD}.json`, where YYYYMMDD is this script's RUN
date — the freeze date — not the date of the underlying articles, since
the P3 sample deliberately spans multiple collection days):

  fixtures/keywords_snapshot_<date>.json
      The full `keywords` table (271 rows, all created 2026-06-24 —
      verified older than every sampled collection day, so using
      "current" keywords to recompute historical P2 output is valid;
      legacy never re-matches on keyword-table changes either, see
      legacy-behavior.md §7 item 6 `rematch_all()` — dead code). Needed
      so the P2 equivalence test (tests/test_p1p2_equivalence.py) can
      rebuild `build_patterns()` input without touching the live DB.

  fixtures/p1_raw_<date>.json
      P1's output contract (legacy-behavior.md §2.6 table): url,
      url_norm, source, title, published, day. Sampled from ONE
      collection day (2026-08-05, the most recent / best-documented run
      in legacy-behavior.md §3, run 110) — 50 matched + 50 unmatched
      titles (seeded random sample within each stratum), so the P2 test
      has real examples of both outcomes, not just the ~19.5% that
      actually match in production. (Deliberately oversampling the
      matched side relative to true 19.5% selectivity — this fixture is
      for behavioral coverage of the match/no-match boundary, not for
      re-deriving selectivity stats, which legacy-behavior.md §4.4
      already covers from the live DB.)

  fixtures/p2_matched_<date>.json
      The SAME 100 rows as p1_raw, with P2's output layered on:
      sections, keywords, review_status, match_type. This is what P2
      actually produced, historically, for these exact titles — the
      target the P2 equivalence test asserts a from-scratch recompute
      (using keywords_snapshot + the ported match_keywords() reference
      in tests/helpers/legacy_reference.py) reproduces exactly.

  fixtures/p3_golden_<date>.json
      The frozen ~100-article parity sample from p3-contract.md §9,
      STRATIFIED by score band (red/yellow/green/magenta) AND by
      language (Bengali-titled / English-titled) per ADR §9.1 + the
      mission brief — a language-blind sample would make parity
      meaningless given the corpus's real ~66% Bengali skew among
      matched/scored articles (measured directly below; the ADR's
      headline "72.4%" figure is over a different population — see
      docs/test-strategy.md for the reconciliation). This is the P3
      parity baseline: real historical `llm_score`/`dup_group`/
      `topic_tag`/... produced by the actually-deployed
      OllamaProvider(qwen3.5:9b) — no LLM call is made by this script.
      `content_text` is truncated to 300 chars (from the legacy cap of
      4000): `judge_batch()` never reads content_text at all (title-only
      prompt), and the only P3 stage that does — `find_duplicates_batch()`
      — truncates its own candidate snippet to exactly `[:300]`
      (p3-contract.md §1.5, `_snip()`). 300 chars is therefore the full
      amount any documented P3 stage ever reads; keeping more would only
      grow the fixture for bytes nothing consumes.
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "news.db"
FIXTURES_DIR = REPO_ROOT / "fixtures"

# Fixed seed for reproducible sampling — re-running this script against an
# unchanged corpus reproduces byte-identical fixtures. Documented, not
# hidden: docs/test-strategy.md cites this same value.
SEED = 20260806

P1_P2_DAY = "2026-08-05"  # run 110 — see legacy-behavior.md §3
P1_P2_SAMPLE_PER_STRATUM = 50  # matched + unmatched, each

# P3 golden sample: score-band x language stratified allocation, 100 total.
# Counts derived from the REAL joint distribution among llm_score IS NOT
# NULL rows (measured 2026-08-06, see docs/test-strategy.md for the raw
# counts) so no stratum is empty and the ~66% Bengali share of the real
# matched/scored pool is preserved rather than flattened to 50/50.
P3_STRATA_TARGETS = {
    ("red", "bn"): 15,
    ("red", "en"): 10,
    ("yellow", "bn"): 17,
    ("yellow", "en"): 8,
    ("green", "bn"): 16,
    ("green", "en"): 9,
    ("magenta", "bn"): 18,
    ("magenta", "en"): 7,
}
CONTENT_TEXT_CAP = 300


def is_bengali(s: str) -> bool:
    """Verbatim port of collector_logic._is_bengali() (collector_logic.py:487-489,
    quoted in legacy-behavior.md §4.2). Bengali Unicode block U+0980-U+09FF.
    """
    return any("ঀ" <= ch <= "৿" for ch in s)


def score_band(score: float) -> str:
    """Verbatim port of main.py's review-UI cutoffs (legacy-behavior.md's
    citation of main.py:172-176 via p3-contract.md §2.2): red >= 8,
    yellow in [6,8), green == 5, magenta < 5.
    """
    if score >= 8:
        return "red"
    if score >= 6:
        return "yellow"
    if score == 5:
        return "green"
    return "magenta"


def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise SystemExit(
            f"{DB_PATH} not found. This script requires a local read-only copy "
            "of the legacy corpus; it is gitignored and never committed. "
            "Nothing under fixtures/ needs this script to run (the committed "
            "JSON is already frozen) — only re-generation does."
        )
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def build_keywords_snapshot(con: sqlite3.Connection, freeze_date: str) -> Path:
    cur = con.execute(
        "SELECT section, term_en, term_bn, enabled FROM keywords WHERE enabled=1 ORDER BY id"
    )
    rows = [dict(r) for r in cur.fetchall()]
    payload = {
        "_meta": {
            "tier": "keywords_snapshot",
            "generated": freeze_date,
            "source": "data/news.db keywords table, read-only SELECT",
            "row_count": len(rows),
            "note": "All rows created_at=2026-06-24, predating every P1/P2/P3 "
            "sample day used elsewhere in fixtures/ — safe to use as 'the "
            "keyword set in effect' for historical recompute.",
        },
        "rows": rows,
    }
    return _write(FIXTURES_DIR / f"keywords_snapshot_{freeze_date}.json", payload)


def _sample_ids(con: sqlite3.Connection, where: str, params: tuple, n: int, rng: random.Random) -> list[int]:
    cur = con.execute(f"SELECT id FROM articles WHERE {where} ORDER BY id", params)
    ids = [r[0] for r in cur.fetchall()]
    if len(ids) < n:
        raise SystemExit(f"Only {len(ids)} candidates for '{where}' — need {n}. Adjust sample size.")
    return sorted(rng.sample(ids, n))


def build_p1_p2(con: sqlite3.Connection, freeze_date: str, rng: random.Random) -> tuple[Path, Path]:
    matched_ids = _sample_ids(
        con, "day=? AND sections != ''", (P1_P2_DAY,), P1_P2_SAMPLE_PER_STRATUM, rng
    )
    unmatched_ids = _sample_ids(
        con, "day=? AND sections = ''", (P1_P2_DAY,), P1_P2_SAMPLE_PER_STRATUM, rng
    )
    all_ids = sorted(matched_ids + unmatched_ids)
    placeholders = ",".join("?" * len(all_ids))

    # P1 output contract, legacy-behavior.md §2.6 table.
    cur = con.execute(
        f"SELECT id, url, url_norm, source, title, published, day "
        f"FROM articles WHERE id IN ({placeholders}) ORDER BY id",
        all_ids,
    )
    p1_rows = [dict(r) for r in cur.fetchall()]

    # P2 output layered on the same rows: sections, keywords, review_status,
    # match_type (legacy-behavior.md §2.6 + §4).
    cur = con.execute(
        f"SELECT id, url, url_norm, source, title, published, day, "
        f"sections, keywords, review_status, match_type "
        f"FROM articles WHERE id IN ({placeholders}) ORDER BY id",
        all_ids,
    )
    p2_rows = [dict(r) for r in cur.fetchall()]

    p1_meta = {
        "tier": "p1_raw",
        "generated": freeze_date,
        "source": f"data/news.db articles table, day='{P1_P2_DAY}', read-only SELECT",
        "sampling": f"seed={SEED}, {P1_P2_SAMPLE_PER_STRATUM} matched + "
        f"{P1_P2_SAMPLE_PER_STRATUM} unmatched, sampled independently within "
        "each stratum then merged",
        "row_count": len(p1_rows),
        "note": "Columns are exactly P1's documented output contract "
        "(legacy-behavior.md §2.6) — no sections/keywords/review_status, "
        "those are P2's output, see p2_matched_<date>.json.",
    }
    p2_meta = dict(p1_meta)
    p2_meta["tier"] = "p2_matched"
    p2_meta["note"] = (
        "Same 100 rows as p1_raw_<date>.json, plus P2's output columns "
        "(sections, keywords, review_status, match_type) as legacy actually "
        "produced them at insert time. tests/test_p1p2_equivalence.py "
        "recomputes sections/keywords from p1_raw + keywords_snapshot via "
        "tests/helpers/legacy_reference.py and asserts equality against "
        "this file."
    )

    p1_path = _write(FIXTURES_DIR / f"p1_raw_{freeze_date}.json", {"_meta": p1_meta, "rows": p1_rows})
    p2_path = _write(FIXTURES_DIR / f"p2_matched_{freeze_date}.json", {"_meta": p2_meta, "rows": p2_rows})
    return p1_path, p2_path


def build_p3_golden(con: sqlite3.Connection, freeze_date: str, rng: random.Random) -> Path:
    cur = con.execute("SELECT id, title, llm_score FROM articles WHERE llm_score IS NOT NULL")
    by_stratum: dict[tuple[str, str], list[int]] = {k: [] for k in P3_STRATA_TARGETS}
    for r in cur.fetchall():
        band = score_band(r["llm_score"])
        lang = "bn" if is_bengali(r["title"]) else "en"
        key = (band, lang)
        if key in by_stratum:
            by_stratum[key].append(r["id"])

    chosen_ids: list[int] = []
    distribution: dict[str, dict] = {}
    for key, target in P3_STRATA_TARGETS.items():
        pool = sorted(by_stratum[key])
        if len(pool) < target:
            raise SystemExit(f"Stratum {key} has only {len(pool)} candidates, need {target}")
        picked = sorted(rng.sample(pool, target))
        chosen_ids.extend(picked)
        distribution[f"{key[0]}/{key[1]}"] = {"target": target, "pool_size": len(pool)}
    chosen_ids = sorted(chosen_ids)

    placeholders = ",".join("?" * len(chosen_ids))
    cur = con.execute(
        f"""SELECT id, title, title_en, source, sections, keywords, content_text,
                   dup_group, is_primary, also_in, match_type, semantic_score,
                   llm_score, llm_reason, topic_tag, topic_standout, topic_parent_id,
                   day
            FROM articles WHERE id IN ({placeholders}) ORDER BY id""",
        chosen_ids,
    )
    rows = []
    for r in cur.fetchall():
        row = dict(r)
        ct = row.get("content_text") or ""
        row["content_text"] = ct[:CONTENT_TEXT_CAP]
        row["content_text_truncated"] = len(ct) > CONTENT_TEXT_CAP
        row["score_band"] = score_band(row["llm_score"])
        row["language"] = "bn" if is_bengali(row["title"]) else "en"
        rows.append(row)

    meta = {
        "tier": "p3_golden",
        "generated": freeze_date,
        "source": "data/news.db articles table, WHERE llm_score IS NOT NULL, "
        "read-only SELECT — real historical output of the actually-deployed "
        "OllamaProvider(qwen3.5:9b), NOT a fresh LLM call by this script.",
        "sampling": f"seed={SEED}, stratified by score_band x language per "
        "P3_STRATA_TARGETS, sampled independently within each of 8 strata "
        "then merged and sorted by id",
        "strata": distribution,
        "row_count": len(rows),
        "content_text_cap": CONTENT_TEXT_CAP,
        "note": "This IS the GATE 4 parity baseline (p3-contract.md §9). "
        "It is NOT, by itself, a valid 'legacy noise floor' run — these rows "
        "were scored inside their original full-day production batches, not "
        "in isolation as a 100-article batch. See docs/test-strategy.md "
        "§ 'Noise-floor measurement design' for why that distinction matters "
        "and what a real noise-floor run must do differently.",
    }
    return _write(FIXTURES_DIR / f"p3_golden_{freeze_date}.json", {"_meta": meta, "rows": rows})


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def main() -> None:
    freeze_date = date.today().strftime("%Y%m%d")
    rng = random.Random(SEED)
    con = _connect()
    try:
        kw_path = build_keywords_snapshot(con, freeze_date)
        p1_path, p2_path = build_p1_p2(con, freeze_date, rng)
        p3_path = build_p3_golden(con, freeze_date, rng)
    finally:
        con.close()

    for p in (kw_path, p1_path, p2_path, p3_path):
        size_kb = p.stat().st_size / 1024
        print(f"wrote {p.relative_to(REPO_ROOT)}  ({size_kb:.1f} KB)")
    print(f"generated_at={datetime.now(timezone.utc).isoformat()} seed={SEED}")


if __name__ == "__main__":
    main()

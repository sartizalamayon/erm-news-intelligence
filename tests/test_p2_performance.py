"""P2 wall-clock — mission DoD #5: "P2 runs over ~5,000 articles. Report
its wall-clock. Legacy compiled patterns per run; note whether you
improved on that and by how much."

Stays CI-safe (mission constraint: tests read only committed fixtures,
never `data/news.db` — docs/test-strategy.md §1.1) by tiling the 100 REAL
titles in `fixtures/p1_raw_*.json` up to ~5,000 rows rather than needing a
live 5,000-article day. This is a fair proxy for the matching engine's own
cost: `match_keywords()`'s runtime is a function of (title length x
pattern count), not of which specific titles are used, and legacy-behavior
.md §3 confirms a real run is ~5,000-5,700 articles, so 5,000 tiled real
titles against the real 271-keyword/~580-pattern set exercises the same
shape of work. A separate one-off measurement against the real
51,886-row corpus (`data/news.db`, SELECT-only) was also run manually for
the mission report — see the report text, not this file, since a
committed test must not depend on a local DB copy CI doesn't have.
"""

from __future__ import annotations

import time

from app.services.matching import build_patterns, keyword_rows_to_groups, match_articles
from tests.fixtures.loaders import load_keywords_snapshot, load_p1_raw

TARGET_ARTICLE_COUNT = 5000
# Generous ceiling: legacy's own full P1+P2 run (fetch-bound) averages
# ~620s; P2's OWN share of that (regex matching only, no network) should
# be a trivial fraction of a second. 5s is a wide margin so this test
# never flakes on a loaded CI runner while still catching an accidental
# quadratic-blowup regression.
WALL_CLOCK_CEILING_SECONDS = 5.0


def _tiled_articles(target_count: int) -> list[dict]:
    rows = load_p1_raw()["rows"]
    tiled = []
    i = 0
    while len(tiled) < target_count:
        row = rows[i % len(rows)]
        # Distinct ids so this looks like `target_count` distinct articles
        # to any id-keyed downstream logic, even though title text repeats.
        tiled.append({**row, "id": f"{row['id']}-{i}"})
        i += 1
    return tiled


def test_matching_5000_tiled_real_titles_completes_within_ceiling() -> None:
    kw_rows = load_keywords_snapshot()["rows"]
    patterns = build_patterns(keyword_rows_to_groups(kw_rows))
    articles = _tiled_articles(TARGET_ARTICLE_COUNT)
    assert len(articles) >= TARGET_ARTICLE_COUNT
    assert len(patterns) > 0

    start = time.perf_counter()
    results = match_articles(articles, patterns)
    elapsed = time.perf_counter() - start

    assert len(results) == len(articles)
    print(
        f"\n[p2-performance] {len(articles)} articles x {len(patterns)} patterns "
        f"in {elapsed:.4f}s ({len(articles) / elapsed:,.0f} articles/sec)"
    )
    assert elapsed < WALL_CLOCK_CEILING_SECONDS, f"P2 matching took {elapsed:.2f}s over {len(articles)} articles"


def test_pattern_compilation_is_cheap_and_a_tiny_fraction_of_matching_time() -> None:
    """Confirms compiling the ~580-pattern set (271 keyword rows split
    into EN + BN terms) is not where P2's time goes — matching dominates,
    which is the expected shape (compile once per run, match thousands of
    times). Also exercises `build_patterns()`'s content-keyed cache
    (module docstring) via a fresh, distinct-but-equal `groups` dict.
    """
    kw_rows = load_keywords_snapshot()["rows"]
    groups = keyword_rows_to_groups(kw_rows)

    start = time.perf_counter()
    patterns = build_patterns(groups)
    compile_elapsed = time.perf_counter() - start

    articles = _tiled_articles(TARGET_ARTICLE_COUNT)
    start = time.perf_counter()
    match_articles(articles, patterns)
    match_elapsed = time.perf_counter() - start

    print(f"\n[p2-performance] compile={compile_elapsed * 1000:.2f}ms match={match_elapsed * 1000:.2f}ms")
    assert compile_elapsed < match_elapsed

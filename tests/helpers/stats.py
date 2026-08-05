"""Pure-Python statistics for the parity harness — no numpy/scipy.

LEAN per the mission brief: rank correlation and partition-agreement are
both a few dozen lines of arithmetic; adding a dependency for them would
be the kind of thing ARCH-REPO's pyproject.toml docstring says to flag
rather than assume. If a later agent wants scipy for something bigger,
that's a separate conversation — these functions are self-contained and
only used by the test harness itself (never by app/).
"""

from __future__ import annotations

from collections import defaultdict


def _ranks(values: list[float]) -> list[float]:
    """Average (fractional) ranks, ties sharing the mean rank — the
    standard input to Spearman's rho. 1-indexed, ascending.
    """
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def spearman_rank_correlation(a: list[float], b: list[float]) -> float:
    """Spearman's rho: Pearson correlation of the two rank sequences.
    Handles ties via average-rank (needed here — llm_score is an integer
    1-10, so ties are the common case, not the exception).

    Returns 0.0 for degenerate input (fewer than 2 points, or either
    series constant — zero variance makes correlation undefined; 0.0 is
    the conservative "no demonstrated agreement" reading rather than
    raising, since a caller computing this over a small manually-curated
    slice of a report is more useful getting a number than a traceback).
    """
    if len(a) != len(b):
        raise ValueError(f"length mismatch: {len(a)} vs {len(b)}")
    n = len(a)
    if n < 2:
        return 0.0
    ra, rb = _ranks(a), _ranks(b)
    mean_a = sum(ra) / n
    mean_b = sum(rb) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb))
    var_a = sum((x - mean_a) ** 2 for x in ra)
    var_b = sum((y - mean_b) ** 2 for y in rb)
    if var_a == 0 or var_b == 0:
        return 0.0
    return cov / (var_a**0.5 * var_b**0.5)


def band_agreement(bands_a: list[str], bands_b: list[str]) -> float:
    """Fraction of positions where the two band sequences agree —
    p3-contract.md §9.2's "same score BAND for a large majority (>=90%)"
    check, as a number rather than a threshold-baked-in boolean.
    """
    if len(bands_a) != len(bands_b):
        raise ValueError(f"length mismatch: {len(bands_a)} vs {len(bands_b)}")
    if not bands_a:
        return 1.0
    agree = sum(1 for x, y in zip(bands_a, bands_b) if x == y)
    return agree / len(bands_a)


def pairwise_coclustering_agreement(labels_a: dict[str, object], labels_b: dict[str, object]) -> float:
    """How often do two partitions of the SAME item set agree on "these
    two items are in the same cluster"? Used for dup_group / topic
    co-location comparisons (p3-contract.md §9.2: "articles the legacy
    system folded together are at LEAST co-located under the same
    rebuilt topic, not necessarily folded identically" — this measures
    exactly that, without requiring cluster ID equality).

    `labels_a`/`labels_b`: {item_id: cluster_label}, same key set required
    (both dicts must cover the same items — raises ValueError otherwise,
    a silent partial comparison would be misleading in a parity report).
    A `None`/missing cluster label is treated as "its own singleton
    cluster" (mirrors dup_group=NULL / topic_tag="" meaning "standalone").

    Returns the fraction of ALL pairs (i, j) where
    "same cluster in A" == "same cluster in B" — 1.0 = identical
    partitions (up to relabeling), a simplified (unweighted) Rand Index.
    Cheap and exact for the ~100-article frozen sample this harness
    targets; not intended for corpus-scale partitions.
    """
    keys_a, keys_b = set(labels_a), set(labels_b)
    if keys_a != keys_b:
        raise ValueError(
            f"partitions cover different item sets: only-in-a={keys_a - keys_b!r} "
            f"only-in-b={keys_b - keys_a!r}"
        )
    items = sorted(keys_a)
    n = len(items)
    if n < 2:
        return 1.0

    def _cluster_of(labels: dict[str, object], item: str, idx: int) -> object:
        val = labels[item]
        # Falsy/None cluster id => singleton, distinct from every other
        # singleton (use the item's own identity as a unique label).
        return val if val not in (None, "") else f"__singleton_{idx}__"

    resolved_a = {item: _cluster_of(labels_a, item, i) for i, item in enumerate(items)}
    resolved_b = {item: _cluster_of(labels_b, item, i) for i, item in enumerate(items)}

    agree = 0
    total = 0
    for i in range(n):
        for j in range(i + 1, n):
            same_a = resolved_a[items[i]] == resolved_a[items[j]]
            same_b = resolved_b[items[i]] == resolved_b[items[j]]
            agree += same_a == same_b
            total += 1
    return agree / total


def cluster_count(labels: dict[str, object]) -> int:
    """Distinct non-singleton-collapsed cluster count, treating each
    None/empty label as its own cluster — matches "topic COUNT" /
    "distinct dup_group count" as used in p3-contract.md §9.2.
    """
    groups: dict[object, int] = defaultdict(int)
    singleton = 0
    for val in labels.values():
        if val in (None, ""):
            singleton += 1
        else:
            groups[val] += 1
    return len(groups) + singleton


def compute_tolerance_from_noise_floor(
    noise_correlation: float,
    noise_band_agreement: float,
    *,
    margin: float = 0.05,
) -> dict[str, float]:
    """Convert a MEASURED legacy self-agreement (two fresh legacy runs
    over the same frozen sample, see docs/test-strategy.md's noise-floor
    design) into the CONFIGURABLE tolerance thresholds the parity harness
    checks the rebuild against.

    Rationale (ADR §7.2 / §9.1): the rebuild should be judged against how
    consistent legacy is with ITSELF, not against a fictional perfect
    reproduction. `margin` is a small forgiveness buffer so a rebuild that
    matches legacy's own noise level doesn't fail by bad luck on one
    sample; it does NOT get to be more generous than that without a
    documented reason (this function does not clamp margin's sign, but a
    negative margin — i.e. demanding BETTER agreement than legacy has with
    itself — is a valid, if strict, choice for the orchestrator to make).

    This is a pure function: it takes measured noise-floor numbers as
    input, never measures them itself (`docs/test-strategy.md`'s
    noise-floor procedure is explicitly designed-not-executed by this
    task — see mission item #4). Nothing calls this with real numbers
    yet; it is exercised in tests with synthetic inputs so the CONVERSION
    logic is verified ahead of the real measurement landing.
    """
    return {
        "rank_correlation_threshold": max(0.0, noise_correlation - margin),
        "band_agreement_threshold": max(0.0, noise_band_agreement - margin),
    }

"""
pipeline.py — the article enrichment pipeline
==============================================
After articles are collected, they pass through a sequence of optional STAGES.
Each stage is independent and can be turned on/off. This is the modular spine
that lets you add the LLM later without touching collection or the UI.

Current stages:
  1. keyword filter   (always on)  — does it match the keywords?
  2. dedup            (optional)   — group same-story articles across outlets
  3. llm_judge        (optional)   — LLM relevance/importance scoring  [future]
  4. llm_summarize    (optional)   — LLM summary                       [future]

Stages 3 and 4 call the pluggable LLM provider (llm.py). Right now the provider
is a no-op, so those stages do nothing — but the wiring is in place, so adding
a model later is just configuring the provider, not rebuilding the pipeline.

Each stage takes the list of article dicts and returns the (possibly modified)
list. Articles are plain dictionaries so any stage can add fields to them
(e.g. an 'llm_score' or 'summary' field) without schema gymnastics.
"""

from __future__ import annotations
from llm import get_provider


# ── AI-only grouping: two-pass batched, with transitive merge ─────────────────
def stage_ai_group(articles, enabled=True, batch_size=12):
    """Group same-event articles using ONLY the LLM (no embeddings).

    Two passes with TRANSITIVE merging (union-find), so a match between ANY two
    members bridges their whole groups — no single-representative blind spot:

      Pass 1: split articles into batches; qwen groups same-event within each.
      Pass 2: re-batch MIXING members across pass-1 groups; whenever qwen says
              two articles are the same event, union their entire groups.

    Conservative: qwen only merges clear same-specific-event stories; everything
    else stays separate. Safe failure direction = under-grouping (a duplicate
    shows twice) rather than wrong merges.
    """
    n = len(articles)
    if not enabled or n < 2:
        for a in articles:
            a.setdefault("dup_group", None)
            a.setdefault("is_primary", 1)
            a.setdefault("also_in", "")
        return articles

    provider = get_provider()
    if not provider.enabled or not hasattr(provider, "verify_group"):
        # no LLM → everything standalone
        for i, a in enumerate(articles):
            a["dup_group"] = None
            a["is_primary"] = 1
            a["also_in"] = ""
        return articles

    # union-find over article indices
    parent = list(range(n))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    def group_indices(idxs):
        """Ask qwen to group a list of article indices; union same-event ones."""
        if len(idxs) < 2:
            return
        headlines = [articles[i].get("title", "") for i in idxs]
        labels = provider.verify_group(headlines)   # same-id = same event
        if len(labels) != len(idxs):
            return
        # union indices that share a label (and label isn't a "unique" singleton)
        from collections import defaultdict
        by_label = defaultdict(list)
        for idx, lab in zip(idxs, labels):
            by_label[lab].append(idx)
        for lab, members in by_label.items():
            for k in range(1, len(members)):
                union(members[0], members[k])

    # ── Pass 1: contiguous batches (duplicates from overlapping sources tend
    # to be collected near each other, so contiguous batching catches many) ──
    order = list(range(n))
    for start in range(0, n, batch_size):
        group_indices(order[start:start + batch_size])

    # ── Pass 2: re-batch across current groups to catch cross-batch dupes ──
    # Build current clusters, then interleave their members into new batches so
    # members of DIFFERENT pass-1 groups are compared together. A single match
    # transitively merges whole groups (union-find handles the rest).
    from collections import defaultdict
    clusters = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)
    reps_round = []
    # take up to 2 members per cluster (so an oddly-worded member can't block a
    # match — gives each group multiple chances to connect)
    for members in clusters.values():
        reps_round.extend(members[:2])
    # shuffle deterministically so different groups mix across batches
    reps_round.sort(key=lambda i: articles[i].get("title", ""))
    for start in range(0, len(reps_round), batch_size):
        group_indices(reps_round[start:start + batch_size])

    # ── assign final group ids + primary/also_in ──
    final = defaultdict(list)
    for i in range(n):
        final[find(i)].append(i)
    gid = 0
    for root, idxs in final.items():
        members = [articles[i] for i in idxs]
        if len(members) == 1:
            members[0]["dup_group"] = None      # standalone
            members[0]["is_primary"] = 1
            members[0]["also_in"] = ""
            continue
        # prefer an English headline as representative
        members.sort(key=lambda m: (0 if _looks_english(m.get("title", "")) else 1))
        for j, m in enumerate(members):
            m["dup_group"] = gid
            m["is_primary"] = 1 if j == 0 else 0
        members[0]["also_in"] = ", ".join(m.get("source", "") for m in members[1:])
        for m in members[1:]:
            m["also_in"] = ""
        gid += 1
    return articles


# ── stage 2: dedup ────────────────────────────────────────────────────────────
def stage_dedup(articles, enabled=True):
    """Group near-duplicate articles (same story, multiple outlets).
    Uses the existing dedup module if available; otherwise passes through."""
    if not enabled or not articles:
        for a in articles:
            a.setdefault("dup_group", None)
            a.setdefault("is_primary", 1)
            a.setdefault("also_in", "")
        return articles
    try:
        import dedup as _dedup
        if _dedup.is_available():
            annotated = _dedup.deduplicate(articles)
            # keep all, but mark non-primary so the UI can collapse them
            for a in annotated:
                a["is_primary"] = 1 if a.get("is_primary", True) else 0
            return annotated
    except Exception as e:
        print(f"  [pipeline] dedup skipped: {e}")
    for a in articles:
        a.setdefault("dup_group", None)
        a.setdefault("is_primary", 1)
        a.setdefault("also_in", "")
    return articles


# ── stage 2b: LLM verifies embedding groups (smart hybrid dedup) ──────────────
def stage_llm_verify_groups(articles, enabled=False):
    """After embedding dedup proposes groups, ask the LLM to check each group:
    are these really the same event? Splits apart wrongly-merged clusters.
    This fixes the over-grouping problem embeddings have. Only groups with 2+
    members are checked (singletons can't be wrong)."""
    if not enabled or not articles:
        return articles
    provider = get_provider()
    if not provider.enabled or not hasattr(provider, "verify_group"):
        return articles

    from collections import defaultdict
    groups = defaultdict(list)
    for a in articles:
        groups[a.get("dup_group")].append(a)

    next_group_id = max((g for g in groups if isinstance(g, int)), default=0) + 1

    for gid, members in groups.items():
        if gid is None or len(members) < 2:
            continue                      # singletons: nothing to verify
        # With a wide-net (low threshold), a candidate cluster can be large.
        # Cap how many headlines go to qwen in one call for reliable output;
        # if a cluster is bigger, process it in chunks (each chunk gets split
        # independently — safe because we only ever SPLIT, never wrongly merge).
        CHUNK = 12
        member_chunks = [members[i:i + CHUNK] for i in range(0, len(members), CHUNK)]
        for chunk in member_chunks:
            if len(chunk) < 2:
                continue
            headlines = [(m.get("title_en") or m.get("title", "")) for m in chunk]
            sub = provider.verify_group(headlines)      # e.g. [0,1,0]
            # each distinct sub-id in this chunk becomes its own fresh group
            subgroup_to_gid = {}
            for m, s in zip(chunk, sub):
                if s not in subgroup_to_gid:
                    subgroup_to_gid[s] = next_group_id
                    next_group_id += 1
                m["dup_group"] = subgroup_to_gid[s]

    # recompute is_primary + also_in per (possibly new) group.
    # IMPORTANT: dup_group=None means "ungrouped singleton" — each such article
    # is its OWN story and must NOT be collapsed with other None articles.
    regrouped = defaultdict(list)
    for a in articles:
        gid = a.get("dup_group")
        if gid is None:
            # give each ungrouped article a private key so it stays standalone
            a["is_primary"] = 1
            a["also_in"] = ""
            continue
        regrouped[gid].append(a)
    for gid, members in regrouped.items():
        # prefer an English-language headline as the representative
        members.sort(key=lambda m: (0 if _looks_english(m.get("title", "")) else 1))
        for i, m in enumerate(members):
            m["is_primary"] = 1 if i == 0 else 0
        others = [m.get("source", "") for m in members[1:]]
        members[0]["also_in"] = ", ".join(others)
        for m in members[1:]:
            m["also_in"] = ""
    return articles


def _looks_english(text):
    """True if the headline is mostly ASCII/Latin (so we prefer it as the
    English representative of a group)."""
    if not text:
        return False
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return ascii_n / max(len(text), 1) > 0.6


# ── stage 0: translate non-English titles → title_en ─────────────────────────
def stage_translate(articles, enabled=True, progress=None):
    """For any article whose title isn't (mostly) English, ask the LLM for an
    English translation and store it as 'title_en'. Runs BEFORE dedup and
    judging so both compare same-language text — closing the cross-lingual gap
    where a Bengali headline and its English counterpart score too low to
    group, and letting the (English-only) relevance guidance apply uniformly.
    English articles get title_en = title (no LLM call, free).
    Already-translated articles (title_en already set — cached from a prior
    run on the same DB row) are skipped, never redone.
    Failures fall back to the original title — never blocks the pipeline.
    The COLLECTOR itself is untouched by this — this stage runs strictly
    after collection, before dedup.
    `progress(done, total, step)` is called after each article so the UI
    bar actually moves rather than appearing frozen for the whole batch."""
    if not enabled or not articles:
        for a in articles:
            a.setdefault("title_en", a.get("title", ""))
        return articles
    provider = get_provider()
    total = len(articles)
    done = 0
    for a in articles:
        if a.get("title_en"):
            done += 1
            if progress:
                progress(done, total, "Translating")
            continue  # already translated (cached)
        title = a.get("title", "")
        if _looks_english(title):
            a["title_en"] = title
        elif provider.enabled and hasattr(provider, "translate_title"):
            translated = provider.translate_title(title)
            a["title_en"] = translated or title  # fall back to original on failure
        else:
            a["title_en"] = title  # no LLM available
        done += 1
        if progress:
            progress(done, total, "Translating")
    return articles


# ── stage 3: LLM judgment — relevance to Grameenphone (1–10) ──────────────────
def stage_llm_judge(articles, enabled=False):
    """Ask the LLM how relevant each article is to Grameenphone (1–10). Adds
    'llm_score' and 'llm_reason'. Uses title_en (the English translation) when
    available, so the English-only relevance guidance applies consistently to
    Bengali articles too. No-op if no model is configured."""
    if not enabled or not articles:
        return articles
    provider = get_provider()
    if not provider.enabled:
        return articles
    for a in articles:
        title_for_scoring = a.get("title_en") or a.get("title", "")
        j = provider.judge(title_for_scoring, a.get("source", ""),
                            a.get("sections", ""))
        a["llm_score"] = j.score
        a["llm_reason"] = j.reason
        a["llm_relevant"] = 1 if j.relevant else 0
    return articles


# ── the orchestrator: run the configured stages in order ──────────────────────
def enrich(articles, config: dict | None = None):
    """Run articles through the enabled stages, in order:
       translate → dedup (embeddings) → LLM verify groups → LLM judge
    (Per-article LLM summarization was removed — a 2-line "summary" of a bare
    headline added no real information. The report generator now fetches full
    article content directly for its bullets instead — see report.py.)
    `config` controls which stages run, e.g.:
        {"translate": True, "dedup": True, "llm_verify": True, "llm_judge": True}
    Returns the enriched article list.
    """
    config = config or {}
    articles = stage_translate(articles, enabled=config.get("translate", True))
    articles = stage_dedup(articles, enabled=config.get("dedup", True))
    articles = stage_llm_verify_groups(articles, enabled=config.get("llm_verify", False))
    articles = stage_llm_judge(articles, enabled=config.get("llm_judge", False))
    return articles

"""
Semantic Deduplication for ERM Daily Risk Intelligence
=======================================================
Groups near-duplicate articles (the same story reported by multiple outlets)
using multilingual sentence embeddings. Works across Bengali AND English, so
a Bengali Prothom Alo headline and an English Daily Star headline about the
same event cluster together.

Approach:
  1. Embed each headline with a multilingual sentence-transformer model.
  2. Compare embeddings; group headlines whose cosine similarity ≥ threshold.
  3. Pick one "representative" per group (prefer English, then longest title),
     and attach an "also reported by" list of the other outlets.

This module is OPTIONAL. If sentence-transformers / torch are not installed,
news_collector.py detects that and skips dedup gracefully.

Install (one-time, ~300-500 MB):
    pip install sentence-transformers
First run downloads the model and caches it.

MODEL CHOICE: the default (paraphrase-multilingual-MiniLM-L12-v2) is small and
fast but UNDER-DISCRIMINATES on headline pairs that mean the same thing in
very different words (e.g. "Grameenphone wins spectrum auction" vs "GP
secures spectrum in BTRC auction" scores only ~0.38 similarity with it — too
low for any sane threshold to catch). Stronger multilingual models score real
duplicates much higher. Override via the EMBED_MODEL env var, e.g.:
    set EMBED_MODEL=intfloat/multilingual-e5-large
    set EMBED_MODEL=BAAI/bge-m3
Both are larger downloads (~1-2GB) but discriminate far better, including on
Bengali. Use compare_models.py (sibling script) to test before committing.
"""
import os

# bge-m3 won the model comparison (compare_models.py): it's the only model with
# a clean SEPARATION between real duplicates and unrelated stories —
#   real dupes (EN/EN, EN/BN, BN/BN): 0.64-0.71
#   unrelated pairs:                   0.34-0.46
# The old MiniLM scored real dupes at 0.38 (unusable); e5-large scored EVERYTHING
# 0.74-0.92 (no gap, would over-merge). bge-m3 + threshold 0.55 sits in the gap.
# Override with EMBED_MODEL / EMBED_THRESHOLD env vars if needed.
MODEL_NAME = os.environ.get("EMBED_MODEL", "BAAI/bge-m3")
# e5 models want "query: "/"passage: " prefixes on every input for best results
_E5_PREFIX = "query: " if "e5" in MODEL_NAME.lower() else ""
DEFAULT_THRESHOLD = float(os.environ.get("EMBED_THRESHOLD", "0.82"))
# A pair at or above this score is treated as a duplicate outright, even
# without a shared keyword (very high semantic match). Between the two
# thresholds, a pair ALSO needs a shared keyword to merge — this stops one
# weak cross-topic link from chaining two unrelated clusters together
# through union-find.
HIGH_CONFIDENCE = float(os.environ.get("EMBED_HIGH_CONFIDENCE", "0.90"))
# Minimum pairwise similarity allowed INSIDE a finished group. Union-find is
# single-linkage, so a chain a~b~c can weld items whose direct similarity is
# low; members are evicted until every remaining pair clears this floor.
# Unset = tracks the threshold (threshold - 0.08).
_FLOOR_ENV = os.environ.get("EMBED_COHERENCE_FLOOR", "")
# Set DEDUP_DEBUG=0 to silence per-pair merge/evict logging (summary stays).
DEDUP_DEBUG = os.environ.get("DEDUP_DEBUG", "1") != "0"


def _log(msg):
    """Console log that never crashes on exotic characters (Windows consoles
    can choke on Bengali under some codepages)."""
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


def _strip_source_suffix(text):
    """Remove a trailing ' - Source Name' / ' - domain.com' tag that gnews and
    some feeds append to headlines, so variants of the same headline match in
    both Layer 1 (near-exact) and Layer 2 (embeddings) without wasting a merge
    on formatting noise. Splits at the LAST spaced separator so hyphenated
    domains (en.bd-pratidin.com) are handled. Conservative: only strips when
    the tail looks like a source tag (domain-like, or at most 4 words) and
    enough headline remains."""
    import re
    if not text:
        return text
    last = None
    for m in re.finditer(r"\s+[-\u2013\u2014|]\s+", text):
        last = m
    if not last:
        return text
    head, tail = text[:last.start()], text[last.end():].strip()
    if len(tail) <= 60 and (("." in tail) or (len(tail.split()) <= 4)):
        if len(head.split()) >= 3:
            return head
    return text


_model = None  # lazy-loaded singleton


def is_available():
    """True if the embedding stack is installed."""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        print(f"  [dedup] loading model {MODEL_NAME} (first run downloads it)...")
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def _prep(text):
    """Apply model-specific input prefixing (e5 family needs this)."""
    return f"{_E5_PREFIX}{text}" if _E5_PREFIX else text


def _prefer_english(title):
    """Score for choosing a group representative: English titles preferred,
    then longer (more descriptive) titles."""
    is_bengali = any("\u0980" <= ch <= "\u09FF" for ch in title)
    return (0 if is_bengali else 1, len(title))


def _normalize(text):
    """Normalize a title for near-exact matching (Layer 1).
    Strips trailing source tags ('… - Dhaka Tribune'), lowercases, strips
    punctuation, collapses whitespace, removes common filler words. Two
    headlines that normalize identically are the same story regardless of
    source — no embedding needed."""
    import re
    t = _strip_source_suffix(text).lower()
    t = re.sub(r"[^\w\s]", " ", t)          # punctuation → space
    t = re.sub(r"\s+", " ", t).strip()
    # strip very common filler that varies across outlets
    stop = {"the", "a", "an", "in", "of", "on", "at", "to", "and", "or",
            "for", "with", "by", "from", "is", "are", "was", "were"}
    words = [w for w in t.split() if w not in stop]
    return " ".join(words)


def _union_find_groups(n, pairs):
    """Union-find (disjoint-set) over n items. `pairs` is an iterable of
    (i, j) index pairs that should be merged. Returns group_of[i] for each i.
    Ensures A~B and B~C both land in one group even if A and C are just
    below the threshold — the key fix over greedy seed-only clustering."""
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(x, y):
        parent[find(x)] = find(y)

    for i, j in pairs:
        union(i, j)

    # normalise to contiguous group ids
    root_to_gid = {}
    group_of = []
    for i in range(n):
        r = find(i)
        if r not in root_to_gid:
            root_to_gid[r] = len(root_to_gid)
        group_of.append(root_to_gid[r])
    return group_of


def deduplicate(articles, threshold=DEFAULT_THRESHOLD):
    """Cluster near-duplicate articles.

    Three-layer pipeline — each layer only sees what the previous missed:

    Layer 1 — Near-exact title matching (free, no ML).
      Normalised titles (lowercase, no punctuation/stop-words) that are
      identical or share ≥85% of their words are the same story. Catches
      wire-service articles published verbatim across multiple outlets without
      touching the embedding model at all.

    Layer 2 — Union-find on bge-m3 embedding similarity.
      For the remaining articles, embed title_en (English-uniform thanks to
      stage_translate) and merge any pair scoring ≥ threshold. Union-find
      ensures transitive grouping: A~B and B~C → one group, even if A and C
      score just below threshold directly. Replaces the old greedy seed-only
      clustering that missed this case.

    Layer 3 — qwen verification (unchanged, lives in stage_llm_verify_groups).
      Checks each proposed group and splits wrongly-merged clusters.

    Input:  list of dicts, each with at least 'title' and 'source'.
    Output: same list annotated with dup_group / is_primary / also_in / dup_count.
    """
    if not articles:
        return articles
    if not is_available():
        for i, a in enumerate(articles):
            a.update(dup_group=i, is_primary=True, also_in="", dup_count=1)
        return articles

    n = len(articles)
    merge_pairs = []   # (i, j) pairs that belong in the same group

    # ── LAYER 1: near-exact title matching ───────────────────────────────────
    from collections import defaultdict
    norm_to_idxs = defaultdict(list)
    for i, a in enumerate(articles):
        key = _normalize(a.get("title_en") or a.get("title", ""))
        if key:
            norm_to_idxs[key].append(i)

    for idxs in norm_to_idxs.values():
        if len(idxs) < 2:
            continue
        # all articles with the same normalised title → merge pair-wise
        for k in range(len(idxs) - 1):
            merge_pairs.append((idxs[k], idxs[k + 1]))

    # ── LAYER 2: union-find on embedding similarity ───────────────────────────
    import numpy as np
    model = _get_model()
    # strip '… - Source' tags so formatting noise doesn't depress similarity
    titles = [_strip_source_suffix(a.get("title_en") or a["title"]) for a in articles]
    prepped = [_prep(t) for t in titles]
    emb = model.encode(prepped, normalize_embeddings=True, show_progress_bar=False)
    sim = emb @ emb.T

    # Cross-lingual guard. bge-m3 maps Bengali and English into ONE vector space
    # and scores loosely-related cross-lingual pairs surprisingly high. An article
    # with no translation is embedded in raw Bengali; without this guard it wrongly
    # merges with unrelated English articles. So flag which titles are still
    # Bengali (untranslated) and refuse any merge that crosses the language line —
    # a real duplicate will regroup on the next run once it has a title_en.
    def _is_bengali(a):
        # if title_en is present and non-empty, it was translated → English side
        te = (a.get("title_en") or "").strip()
        if te:
            return False
        return any("\u0980" <= ch <= "\u09FF" for ch in a.get("title", ""))
    lang_bn = [_is_bengali(a) for a in articles]

    floor = float(_FLOOR_ENV) if _FLOOR_ENV else max(0.0, threshold - 0.08)
    _log(f"  [dedup] model={MODEL_NAME} threshold={threshold} "
         f"high_conf={HIGH_CONFIDENCE} coherence_floor={floor:.2f} articles={n} "
         f"held_out_untranslated={sum(lang_bn)}")

    # Guard tokens: KEYWORDS ONLY. Sections were removed deliberately — every
    # 'Macroeconomy' article shares that token with every other, which made
    # the guard a no-op exactly where the bad groups formed. Keywords are
    # narrower, though still broad ('iran', 'bb'); the coherence floor below
    # is the structural defense, this guard is a secondary filter.
    def _tokens(a):
        raw = str(a.get("keywords", "")).lower()
        return {t.strip() for t in raw.replace(";", ",").split(",") if t.strip()}
    tok = [_tokens(a) for a in articles]

    for i in range(n):
        for j in range(i + 1, n):
            # Any article still lacking an English title (translation hasn't run
            # or failed) is embedded in raw Bengali, where bge-m3 similarity is
            # loose and unreliable — it produces wrong merges, including
            # Bengali↔Bengali ones. Hold such articles OUT of grouping entirely:
            # they stay singletons this run and regroup correctly once translated.
            if lang_bn[i] or lang_bn[j]:
                continue
            s = sim[i, j]
            if s >= HIGH_CONFIDENCE:
                merge_pairs.append((i, j))
                if DEDUP_DEBUG:
                    _log(f"  [dedup] merge {s:.2f} (high) "
                         f"'{titles[i][:48]}' <-> '{titles[j][:48]}'")
            elif s >= threshold:
                shared = tok[i] & tok[j]
                if shared:
                    merge_pairs.append((i, j))
                    if DEDUP_DEBUG:
                        _log(f"  [dedup] merge {s:.2f} (kw:{sorted(shared)[0]}) "
                             f"'{titles[i][:48]}' <-> '{titles[j][:48]}'")

    # ── MERGE via union-find (layers 1 + 2 combined) ─────────────────────────
    group_of = _union_find_groups(n, merge_pairs)

    members = defaultdict(list)
    for i, g in enumerate(group_of):
        members[g].append(i)

    # ── COHERENCE CHECK: break chain-welded groups ────────────────────────────
    # Union-find is single-linkage: one borderline pair can weld two unrelated
    # clusters (a~b and b~c group all three even when a and c are strangers).
    # Enforce a floor on the MINIMUM pairwise similarity inside each group:
    # iteratively evict the least-coherent member until every remaining pair
    # clears the floor. Evicted articles become singletons — under-grouping
    # is the safe failure mode for a risk tool.
    next_gid = max(members.keys(), default=-1) + 1
    for g in list(members.keys()):
        idxs = members[g]
        while len(idxs) > 1:
            worst_val, worst_i = min(
                (min(sim[i, j] for j in idxs if j != i), i) for i in idxs)
            if worst_val >= floor:
                break
            idxs.remove(worst_i)
            members[next_gid] = [worst_i]
            next_gid += 1
            if DEDUP_DEBUG:
                _log(f"  [dedup] evict {worst_val:.2f}<{floor:.2f} "
                     f"'{titles[worst_i][:60]}'")

    # ── ANNOTATE ─────────────────────────────────────────────────────────────
    for g, idxs in members.items():
        primary_idx = max(idxs, key=lambda i: _prefer_english(articles[i]["title"]))
        other_sources = sorted({articles[i]["source"] for i in idxs
                                if i != primary_idx})
        for i in idxs:
            articles[i]["dup_group"] = g
            articles[i]["is_primary"] = (i == primary_idx)
            articles[i]["dup_count"] = len(idxs)
            articles[i]["also_in"] = ", ".join(other_sources) if i == primary_idx else ""

    multi = sum(1 for idxs in members.values() if len(idxs) > 1)
    _log(f"  [dedup] {n} articles -> {len(members)} stories ({multi} multi-article groups)")

    return articles


def primary_only(articles):
    """Convenience: return just the representative of each group, with the
    'also reported by' info preserved. Use this for a de-duplicated report."""
    return [a for a in articles if a.get("is_primary", True)]


# ── self-test ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    sample = [
        {"title": "Grameenphone wins spectrum auction as BTRC sets new tariff", "source": "Daily Star"},
        {"title": "GP secures spectrum in BTRC auction", "source": "Dhaka Tribune"},
        {"title": "গ্রামীণফোন স্পেকট্রাম নিলামে জয়ী, বিটিআরসির নতুন মাশুল", "source": "Prothom Alo"},
        {"title": "IMF warns Bangladesh on rising inflation", "source": "CNN"},
        {"title": "Bangladesh inflation concerns flagged by IMF", "source": "Al Jazeera"},
        {"title": "Dhaka stock market closes higher on banking gains", "source": "BD News 24"},
    ]
    if not is_available():
        print("sentence-transformers not installed — install to test clustering.")
    else:
        result = deduplicate(sample)
        from collections import defaultdict
        groups = defaultdict(list)
        for a in result:
            groups[a["dup_group"]].append(a)
        print(f"\n{len(sample)} articles → {len(groups)} unique stories\n")
        for g, items in sorted(groups.items()):
            primary = next(a for a in items if a["is_primary"])
            print(f"GROUP {g} ({primary['dup_count']} outlet(s)):")
            print(f"  PRIMARY: [{primary['source']}] {primary['title']}")
            if primary["also_in"]:
                print(f"  also reported by: {primary['also_in']}")
            print()

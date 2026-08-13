"""Lexical near-duplicate pre-clustering for P3's "Group duplicates".

Why this exists
---------------
P3's dedup stage was designed as two passes: an embedding model
proposes candidate groups, then the LLM splits any group it over-merged
(`enrichment.stage_llm_verify_groups`). The embedding half was never
implemented — `app.services.llm.embedding` ships only
`UnavailableEmbeddingBackend`, whose `is_available()` is hardcoded
`False`. The consequence was invisible rather than loud: with no
backend, `dup_group` stayed `None` on every article, and the LLM verify
pass skips any group whose id is `None` (`if gid is None: continue`).
So the stage ran, reported "done · processed N article(s)", and
detected exactly zero duplicates — every run, forever.

That is the worst kind of failure: a green checkmark on a no-op. This
module replaces the missing half with something that actually works and
that does not depend on a paid/rate-limited embedding API at all.

Approach
--------
Headline-level lexical clustering. Duplicate coverage of one event
across Bangladeshi outlets overwhelmingly shares proper nouns and
numbers ("পেট্রোবাংলা", "Petrobangla", "৳৫০০ কোটি") even when the
surrounding phrasing differs, which is precisely what a token-overlap
measure captures. Deliberately NOT a semantic model: this pass only has
to be a good *proposer*. Over-merging is corrected by the LLM verify
pass that already exists downstream; under-merging is the safe failure
mode (two copies of one story shown separately is a minor annoyance,
merging two unrelated stories hides one entirely). Thresholds below are
therefore tuned to be conservative.

Cost
----
Blocking via an inverted index over rare-ish tokens, so this is not the
O(n^2) all-pairs comparison the naive version would be, and it does
zero network I/O. On a real day (~200 matched articles) it completes in
well under a second.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from app.logging import get_logger

logger = get_logger(__name__)

# Two headlines are proposed as the same story when their token sets
# reach EITHER of these bars:
#   * Jaccard  — symmetric overlap, for headlines of similar length.
#   * Containment — the smaller headline's tokens almost entirely inside
#     the larger's, for the common "wire copy + local expansion" pair
#     where one outlet runs a 6-word version of a 14-word headline.
JACCARD_THRESHOLD = 0.62
CONTAINMENT_THRESHOLD = 0.80

# Headlines this short carry too little signal for overlap to mean
# anything — "Gas crisis deepens" would happily merge with any other
# three-word gas headline. They are left ungrouped rather than guessed.
MIN_TOKENS_FOR_MATCH = 4

# A token appearing in more than this share of the day's headlines is a
# blocking liability, not a signal: it produces one enormous candidate
# block that costs O(n^2) to scan and contributes nothing distinguishing.
# Excluded from BLOCKING only — still counted in similarity.
MAX_BLOCK_TOKEN_DOC_RATIO = 0.18

# Floor under the blocking cap above. Without it the ratio silently
# disables candidate generation entirely on small inputs — at n=3 the
# cap computes to max(2, 0) = 2, so a token shared by all three
# headlines is "too common to block on" and NO pair is ever compared.
# Caught by `test_transitive_clustering_merges_a_chain`, which found
# three near-identical headlines producing zero groups. Blocking is only
# ever an optimisation; it must never be the reason a real duplicate is
# missed. At this floor a full O(n^2) scan is at most ~400 comparisons,
# which is free.
MIN_BLOCK_DF_FLOOR = 20

# Hard ceiling on a proposed group. Nothing legitimate looks like 40
# outlets running one headline; a group that large means the thresholds
# have chained transitively through a hub headline, so it is discarded
# rather than handed to the LLM (and logged, so it is never silent).
MAX_GROUP_SIZE = 40

_WS_RE = re.compile(r"\s+", re.UNICODE)


def _is_word_char(ch: str) -> bool:
    """Keep letters (L*), COMBINING MARKS (M*), and digits (N*).

    The obvious implementation — `re.sub(r"[^\\w\\s]", " ", text)` — is
    wrong for Bengali and was caught by
    `test_tokenize_handles_bengali`. Python's `\\w` follows
    `str.isalnum()`, which is False for Unicode nonspacing marks (Mn).
    Bengali writes its vowels and conjuncts as exactly such marks
    (হসন্ত ্ U+09CD, া U+09BE, ে U+09C7 ...), so `\\w` treated them as
    punctuation and shattered every word: "গ্যাস সংকটে দুঃখ প্রকাশ
    পেট্রোবাংলার" tokenized to {'কট', 'রক'} — the actual words gone,
    two meaningless fragments left. Identical headlines still matched
    (identical inputs shatter identically), which is why the real-data
    spot check looked fine; anything merely SIMILAR in Bengali would
    have silently failed to group, in a corpus that is 72.4%
    Bengali-titled.
    """
    return unicodedata.category(ch)[0] in ("L", "M", "N")

# Function words carry no identifying signal and inflate overlap between
# unrelated headlines. Bengali entries are the high-frequency
# postpositions/conjunctions that appear in a large share of headlines.
_STOPWORDS = frozenset(
    {
        # English
        "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for", "with", "from",
        "by", "as", "is", "are", "was", "were", "be", "been", "will", "has", "have", "had", "its",
        "it", "that", "this", "these", "those", "after", "before", "over", "under", "amid", "says",
        "said", "new", "up", "down", "out", "not", "no", "than", "into", "about",
        # Bengali
        "এবং", "ও", "এর", "এই", "সেই", "করে", "করা", "হয়", "হবে", "হয়েছে", "থেকে", "জন্য",
        "সঙ্গে", "মধ্যে", "না", "কি", "যে", "তার", "তারা", "আরও", "নিয়ে", "পর", "কাছে",
    }
)


def _normalize(text: str) -> str:
    """NFKC first so visually identical Unicode (and the Bengali digits
    and conjuncts these sources mix freely) compares equal, then strip
    punctuation and collapse whitespace.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold()
    stripped = "".join(ch if (ch.isspace() or _is_word_char(ch)) else " " for ch in folded)
    return _WS_RE.sub(" ", stripped).strip()


def tokenize(text: str) -> frozenset[str]:
    """Content tokens of a headline. Single characters are dropped — in
    both scripts they are almost always fragments, not words.
    """
    normalized = _normalize(text)
    if not normalized:
        return frozenset()
    return frozenset(tok for tok in normalized.split(" ") if len(tok) > 1 and tok not in _STOPWORDS)


def _similar(a: frozenset[str], b: frozenset[str]) -> bool:
    if len(a) < MIN_TOKENS_FOR_MATCH or len(b) < MIN_TOKENS_FOR_MATCH:
        return False
    intersection = len(a & b)
    if not intersection:
        return False
    if intersection / len(a | b) >= JACCARD_THRESHOLD:
        return True
    return intersection / min(len(a), len(b)) >= CONTAINMENT_THRESHOLD


class _UnionFind:
    """Standard union-find with path compression — turns pairwise
    "these two match" decisions into transitive clusters in near-linear
    time.
    """

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[max(ra, rb)] = min(ra, rb)


def _headline_of(article: dict[str, Any]) -> str:
    """Prefer the English translation — dedup runs AFTER translate, and
    comparing two Bengali originals against one already-English headline
    would never match on tokens. `enrichment` additionally force-
    translates Bengali stragglers immediately before this stage for the
    same reason.
    """
    return (article.get("title_en") or "").strip() or (article.get("title") or "")


def propose_duplicate_groups(articles: list[dict[str, Any]], *, allocate_id: Callable[[], int]) -> int:
    """Assign `dup_group` in place to near-duplicate headlines.

    Articles with no confident partner get `dup_group = None`, exactly
    as an unavailable embedding backend left them — the downstream LLM
    verify pass treats that as "nothing to split", which is correct.

    `allocate_id` must yield ids that are unique across ALL days, not
    just this run: `articles.dup_group` is read back with an unscoped
    `WHERE dup_group = ANY(...)` (app/api/routers/review.py:332), so a
    per-run counter restarting at 1 would make today's "group 1" and a
    previous day's "group 1" the same group in the reviewer UI. In
    production this is backed by the `articles_dup_group_seq` sequence
    (migrations/0017).

    Returns the number of multi-article groups proposed.
    """
    if len(articles) < 2:
        for article in articles:
            article["dup_group"] = None
        return 0

    tokens = [tokenize(_headline_of(a)) for a in articles]

    # Inverted index, skipping tokens too common to discriminate.
    postings: dict[str, list[int]] = defaultdict(list)
    for i, toks in enumerate(tokens):
        for tok in toks:
            postings[tok].append(i)
    max_df = max(MIN_BLOCK_DF_FLOOR, int(len(articles) * MAX_BLOCK_TOKEN_DOC_RATIO))

    uf = _UnionFind(len(articles))
    compared: set[tuple[int, int]] = set()
    for token, members in postings.items():
        if len(members) < 2 or len(members) > max_df:
            continue
        for pos, i in enumerate(members):
            for j in members[pos + 1 :]:
                pair = (i, j)
                if pair in compared:
                    continue
                compared.add(pair)
                if _similar(tokens[i], tokens[j]):
                    uf.union(i, j)

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(len(articles)):
        clusters[uf.find(i)].append(i)

    proposed = 0
    for members in clusters.values():
        if len(members) < 2:
            for i in members:
                articles[i]["dup_group"] = None
            continue
        if len(members) > MAX_GROUP_SIZE:
            logger.warning(
                "dedup_group_discarded_too_large",
                extra={"size": len(members), "max": MAX_GROUP_SIZE},
            )
            for i in members:
                articles[i]["dup_group"] = None
            continue
        gid = allocate_id()
        for i in members:
            articles[i]["dup_group"] = gid
        proposed += 1

    logger.info(
        "dedup_groups_proposed",
        extra={"articles": len(articles), "groups": proposed},
    )
    return proposed

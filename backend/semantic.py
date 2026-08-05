"""
semantic.py — semantic (embedding-based) second-pass filter.

Purpose
-------
The keyword filter keeps an article only if its title literally contains a
tracked term. This misses paraphrases: "Bangladesh Bank tightens monetary
stance" carries no keyword but is clearly a policy-rate risk story. The semantic
filter catches those by comparing each keyword-FAILED article's English title
against a reference set that represents the risk domain, and keeping the ones
that are close enough.

Design (the "C" plan)
---------------------
Reference set is rebuilt every run from two self-maintaining sources:
  1. keyword-derived phrases  — each tracked keyword expanded into a short
     phrase (always available, even on day one).
  2. rolling approved titles  — title_en of articles the desk approved in the
     last N days (grows richer as analysts do their normal job; no separate
     list to maintain).

Only runs on title_en (English), so Bengali articles are matched on their
translation — sidestepping bge-m3's weaker raw-Bengali behaviour. Bengali
KEYWORDS are still embedded on the reference side (bge-m3 is multilingual),
which is why we keep bge-m3 rather than an English-only model.

Marks matches with match_type: keyword / semantic / both, and stores the
cosine score in semantic_score for tuning.
"""
import os
from datetime import date, timedelta

# reuse the same embedding model instance the dedup stage loads
import dedup as _dedup

SEMANTIC_THRESHOLD = float(os.environ.get("SEMANTIC_THRESHOLD", "0.75"))
APPROVED_WINDOW_DAYS = int(os.environ.get("SEMANTIC_APPROVED_DAYS", "60"))
DEBUG = os.environ.get("SEMANTIC_DEBUG", "1") != "0"


def _log(*a):
    if DEBUG:
        print(*a)


# ── keyword → phrase expansion ────────────────────────────────────────────────
# A bare token ("forex") embeds poorly; a short domain phrase embeds with real
# meaning. We map each report section to a light context suffix so the phrase
# lands near the kind of story that section is about.
_SECTION_CONTEXT = {
    "Geopolitical": "geopolitical conflict and international relations",
    "Macroeconomic": "Bangladesh macroeconomy, inflation, reserves and growth",
    "Trade & Business": "trade, business, exports and industry in Bangladesh",
    "Telecom": "Bangladesh telecom sector and BTRC regulation",
    "Budget": "Bangladesh national budget and fiscal policy",
    "Political": "Bangladesh politics and governance",
    "Weather & Environmental": "weather, flooding and environmental risk in Bangladesh",
}


def build_reference_set(conn, keyword_rows):
    """Return (texts, meta) for the reference set.
    keyword_rows: iterable of (term_en, term_bn, section).
    meta is a parallel list describing each reference item for debugging."""
    texts, meta = [], []

    # 1. keyword-derived phrases (English + Bengali terms both used, since the
    #    model is multilingual on the reference side)
    for term_en, term_bn, section in keyword_rows:
        ctx = _SECTION_CONTEXT.get(section, "")
        for term in (term_en, term_bn):
            term = (term or "").strip()
            if not term:
                continue
            phrase = f"{term} — {ctx}" if ctx else term
            texts.append(phrase)
            meta.append(("kw", section, term))

    # 2. rolling approved titles (English only — title_en)
    cutoff = (date.today() - timedelta(days=APPROVED_WINDOW_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT title_en FROM articles
           WHERE review_status='approved'
             AND title_en IS NOT NULL AND title_en != ''
             AND day >= ?""",
        (cutoff,)).fetchall()
    for r in rows:
        t = (r[0] or "").strip()
        if t:
            texts.append(t)
            meta.append(("approved", "", t[:50]))

    return texts, meta


def run_semantic_filter(conn, articles, keyword_rows, progress=None):
    """Score keyword-FAILED articles against the reference set; mark high
    matches by setting a['match_type']='semantic' and a['semantic_score'].
    Returns count of new semantic matches. Does NOT write to the DB — the
    caller persists.

    The embedding model is bilingual, so each article is scored on its NATIVE
    title: English articles on title_en (or title), Bengali articles on their
    Bengali title directly. The reference set carries both EN and BN keyword
    phrases so each side matches in-language.
    """
    def _native_title(a):
        # prefer an English translation when present; otherwise the raw title
        # (which for Bengali articles is Bengali — matched against BN phrases)
        te = (a.get("title_en") or "").strip()
        return te if te else (a.get("title") or "").strip()

    candidates = [a for a in articles if _native_title(a)]
    if not candidates:
        _log("  [semantic] no candidates with a usable title")
        return 0

    texts, meta = build_reference_set(conn, keyword_rows)
    if not texts:
        _log("  [semantic] empty reference set — skipping")
        return 0

    import numpy as np
    model = _dedup._get_model()
    _log(f"  [semantic] threshold={SEMANTIC_THRESHOLD} "
         f"reference={len(texts)} ({sum(1 for m in meta if m[0]=='kw')} kw-phrases + "
         f"{sum(1 for m in meta if m[0]=='approved')} approved) "
         f"candidates={len(candidates)}")

    ref_emb = model.encode([_dedup._prep(t) for t in texts],
                           normalize_embeddings=True, show_progress_bar=False)
    cand_titles = [_dedup._prep(_native_title(a)) for a in candidates]
    cand_emb = model.encode(cand_titles, normalize_embeddings=True,
                            show_progress_bar=False)

    sims = cand_emb @ ref_emb.T           # (n_cand, n_ref)
    best = sims.max(axis=1)
    best_idx = sims.argmax(axis=1)

    new_matches = 0
    for a, score, ri in zip(candidates, best, best_idx):
        a["semantic_score"] = float(score)
        if score >= SEMANTIC_THRESHOLD:
            a["match_type"] = "semantic"
            new_matches += 1
            if DEBUG:
                ref_kind, ref_sec, ref_txt = meta[ri]
                _log(f"  [semantic] {score:.2f} '{_native_title(a)[:50]}' "
                     f"~ [{ref_kind}] '{ref_txt}'")

    _log(f"  [semantic] {new_matches} new semantic matches "
         f"of {len(candidates)} candidates")
    return new_matches

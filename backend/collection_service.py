"""
collection_service.py — bridge between the collector and the app database
==========================================================================
This runs a news collection FROM the app: it reads the sources and keywords
out of the database (where we migrated them), fetches articles using the
existing collector logic, runs them through the enrichment pipeline (dedup +
the modular LLM stages), and stores the results in the app's 'articles' table
as 'pending' for review.

It also records each run in the 'runs' table so the debug dashboard can show
history and per-source results.

The heavy work happens in a BACKGROUND thread (see jobs.py) so the web page
never freezes while collection runs.
"""

import json
import re
from datetime import date, datetime, timezone, timedelta

from database import get_connection
import collector_logic as clog
from pipeline import enrich

DHAKA = timezone(timedelta(hours=6))


# ── build sources & keywords from the DATABASE (not code) ─────────────────────
def _sources_from_db():
    """Read enabled sources from the database and rebuild the dict shape the
    collector expects (name + method + the JSON config fields)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT name, method, config FROM sources WHERE enabled=1 ORDER BY name"
    ).fetchall()
    conn.close()
    sources = []
    for r in rows:
        src = {"name": r["name"], "method": r["method"]}
        cfg = json.loads(r["config"] or "{}")
        src.update(cfg)          # merge url / urls / fallbacks / combine etc.
        sources.append(src)
    return sources


def _patterns_from_db():
    """Read enabled keywords from the database and build the regex patterns the
    collector's matcher uses. Mirrors collector_logic.build_patterns but sourced
    from the DB instead of Excel."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT section, term_en, term_bn FROM keywords WHERE enabled=1"
    ).fetchall()
    conn.close()

    # group terms by section, like the collector expects: {section: [(term,label)]}
    groups = {}
    for r in rows:
        section = r["section"]
        groups.setdefault(section, [])
        if (r["term_en"] or "").strip():
            groups[section].append((r["term_en"].strip(), r["term_en"].strip()))
        for bn in (r["term_bn"] or "").split(","):
            bn = bn.strip()
            if bn:
                # label Bengali matches under the English term for the report
                label = r["term_en"].strip() or bn
                groups[section].append((bn, label))
    return clog.build_patterns(groups)


# ── the main collection routine ───────────────────────────────────────────────
def run_collection(trigger="manual", progress=None, should_stop=None):
    """Fetch from all enabled sources, filter by keywords, enrich, and store.
    `progress` is an optional callback(done, total, source_name) for live UI.
    Returns a summary dict.
    """
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO runs (trigger, status) VALUES (?, 'running')", (trigger,))
    run_id = cur.lastrowid
    conn.commit()

    sources = _sources_from_db()
    patterns = _patterns_from_db()
    today = date.today().isoformat()

    total_new = 0
    total_matched = 0
    details = {}
    matched_articles = []   # collect matched ones to enrich together

    # ── PHASE 1: fetch all sources in PARALLEL (network I/O — the slow part) ──
    # SQLite can't take concurrent writes, so we only parallelize the fetching.
    # Each worker returns (name, articles, method_used, error); the main thread
    # then writes everything to the DB sequentially in PHASE 2.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    FETCH_WORKERS = 8

    def _fetch_source(src):
        name = src["name"]
        try:
            articles, method_used = clog.fetch_all_methods(src)
            return (name, articles, method_used, None)
        except Exception as e:
            return (name, [], "", str(e))

    fetch_results = []
    done_count = 0
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_source, src): src for src in sources}
        for fut in as_completed(futures):
            name, articles, method_used, err = fut.result()
            fetch_results.append((name, articles, method_used, err))
            done_count += 1
            if progress:
                progress(done_count, len(sources), name)
            # Force Stop: abandon remaining fetches. Those already completed are
            # still written below so nothing collected is lost.
            if should_stop and should_stop():
                break

    # ── PHASE 2: write everything to the DB (main thread, sequential) ──
    for name, articles, method_used, err in fetch_results:
        src_new = src_matched = 0
        if err:
            details[name] = {"method": "", "new": 0, "matched": 0,
                             "status": f"error: {err}"}
            continue
        try:
            undated_kept = 0
            dropped_old = 0
            dropped_dupe = 0
            for title, url, dt in articles:
                title = clog.clean_title(title)   # backstop: never store raw HTML
                age = clog.is_today(dt)
                if age is False:
                    dropped_old += 1
                    continue                      # confidently old → skip
                if age is None:
                    undated_kept += 1
                    if undated_kept > 15:
                        continue
                url_norm = clog.normalize_url(url)
                exists = conn.execute(
                    "SELECT 1 FROM articles WHERE url=? OR url_norm=?",
                    (url, url_norm)).fetchone()
                if exists:
                    dropped_dupe += 1
                    continue
                sections, keywords = clog.match_keywords(title, patterns)
                pub = (dt.astimezone(DHAKA).strftime("%Y-%m-%d %H:%M")
                       if dt else "")
                is_match = bool(sections)
                conn.execute(
                    """INSERT INTO articles
                       (url, url_norm, source, title, published, day, sections, keywords,
                        review_status, match_type)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (url, url_norm, name, title, pub, today, sections, keywords,
                     "pending" if is_match else "filtered_out",
                     "keyword" if is_match else "")
                )
                src_new += 1
                total_new += 1
                if is_match:
                    src_matched += 1
                    total_matched += 1
                    matched_articles.append(
                        {"url": url, "title": title, "source": name,
                         "sections": sections, "keywords": keywords})
            details[name] = {"method": method_used, "new": src_new,
                             "matched": src_matched, "status": "ok",
                             "fetched": len(articles), "dropped_old": dropped_old,
                             "dupe": dropped_dupe}
        except Exception as e:
            details[name] = {"method": "", "new": 0, "matched": 0,
                             "status": f"error: {e}"}
    try:
        conn.commit()

        # NOTE: no dedup here. Fetch is standalone and fast — articles appear
        # ungrouped immediately. ALL enrichment (dedup + relevance + summaries)
        # runs afterward in run_llm_enrichment() so the fetch never blocks.

        # ── fetch article bodies for the keyword-matched pool ──
        # Only Pool 2 (matched articles), not the full collected set, so this
        # stays fast (~500-700 fetches, not ~5000). Bodies are stored in
        # content_text for use in relevance scoring and report generation.
        if matched_articles and not (should_stop and should_stop()):
            if progress:
                progress(len(sources), len(sources), "Fetching article bodies")
            try:
                today_matched = conn.execute(
                    """SELECT id, url, content_text FROM articles
                       WHERE day=? AND review_status='pending'""",
                    (today,)).fetchall()
                body_rows = [dict(r) for r in today_matched]
                _fetch_bodies(conn, body_rows, progress)
            except Exception as e:
                print(f"[collection] body fetch failed (non-fatal): {e}")

        # finalize the run record
        conn.execute(
            """UPDATE runs SET finished_at=datetime('now'), status='done',
               total_new=?, total_matched=?, details=? WHERE id=?""",
            (total_new, total_matched, json.dumps(details), run_id)
        )
        conn.commit()
    except Exception as e:
        # mark the run errored instead of leaving it stuck 'running'
        try:
            conn.execute(
                "UPDATE runs SET finished_at=datetime('now'), status=? WHERE id=?",
                (f"error: {e}", run_id))
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    return {"run_id": run_id, "total_new": total_new,
            "total_matched": total_matched, "sources": len(sources),
            "details": details,
            "cancelled": bool(should_stop and should_stop())}


def run_llm_enrichment(progress=None, stages=None, should_stop=None):
    """Run the AI pipeline over today's pending articles.

    `stages` selects which stages run. None = all (translate, dedup, judge),
    the normal full run. Pass a set/list like {"translate"}, {"dedup"}, or
    {"judge"} to run just one stage on demand (e.g. re-run grouping after a
    dedup fix without re-translating or re-scoring).

    Row selection differs by stage so a single-stage re-run works correctly:
      - translate: articles missing title_en
      - dedup:     ALL of today's pending articles (regroup from scratch)
      - judge:     articles missing llm_score (unless dedup/full run, which
                   re-selects; a standalone judge run scores only unscored)

    Runs in the background. Updates the DB as it goes so the review UI shows
    results filling in live. `progress` is an optional callback
    (done, total, step_label) for the progress bar.
    """
    import os
    from datetime import date
    from llm import get_provider
    from pipeline import stage_dedup, stage_llm_verify_groups, stage_translate

    provider = get_provider()
    if not provider.enabled:
        return {"enriched": 0, "skipped": "LLM not enabled"}

    # which stages to run — None means "all, driven by env vars" (full run)
    full_run = stages is None
    want = set(stages) if stages else set()
    do_translate = ("translate" in want) if not full_run else (os.environ.get("LLM_TRANSLATE", "1") != "0")
    do_dedup     = ("dedup" in want)     if not full_run else (os.environ.get("LLM_DEDUP", "1") != "0")
    do_judge     = ("judge" in want)     if not full_run else (os.environ.get("LLM_JUDGE", "1") != "0")
    do_bodies    = ("bodies" in want)    if not full_run else False  # bodies only on explicit request or full run
    do_semantic  = ("semantic" in want)  if not full_run else (os.environ.get("LLM_SEMANTIC", "1") != "0")
    do_topic     = ("topic" in want)     if not full_run else (os.environ.get("LLM_TOPIC", "1") != "0")

    conn = get_connection()
    try:
        today = date.today().isoformat()

        # Row selection: a standalone dedup run must see ALL pending articles so
        # it can regroup from scratch; translate/judge only need their pending
        # subset. For a full run we keep the original "unscored" scope so it
        # behaves exactly as before.
        if not full_run and do_bodies and not do_dedup and not do_judge and not do_translate:
            # standalone body fetch: pending articles missing content_text
            where = "day=? AND review_status='pending' AND (content_text IS NULL OR content_text='')"
        elif not full_run and do_dedup and not do_judge and not do_translate:
            # standalone grouping: pull every pending article today
            where = "day=? AND review_status='pending'"
        elif not full_run and do_translate and not do_dedup and not do_judge:
            # standalone translate: only those missing a translation
            where = "day=? AND review_status='pending' AND (title_en IS NULL OR title_en='')"
        elif not full_run and do_topic and not do_dedup and not do_judge and not do_translate and not do_semantic:
            # standalone topic clustering: doesn't use the `articles` list at all
            # (STEP 3 queries scored primaries fresh from the DB), but we still
            # need a non-empty selection so the early `if not total` guard below
            # doesn't return before STEP 3 runs. Pull today's pending articles.
            where = "day=? AND review_status='pending'"
        else:
            # full run, or standalone judge: unscored pending articles
            where = "day=? AND review_status='pending' AND llm_score IS NULL"

        rows = conn.execute(
            f"""SELECT id, url, source, title, title_en, sections, keywords, dup_group,
                       is_primary, also_in, content_text
                FROM articles
                WHERE {where}""",
            (today,)).fetchall()
        articles = [dict(r) for r in rows]
        total = len(articles)
        if not total and not do_topic:
            # nothing to translate/dedup/judge. (do_topic is exempt: STEP 3
            # queries scored primaries straight from the DB and doesn't use
            # `articles`, so an empty selection must NOT skip it.)
            return {"enriched": 0}

        # ── STEP 0: TRANSLATE non-English titles → title_en ──
        if do_translate and not (should_stop and should_stop()):
            def _translate_progress(done, total, step):
                if progress:
                    progress(done, total, step)
            articles = stage_translate(articles, enabled=True,
                                       progress=_translate_progress)
            for a in articles:
                conn.execute("UPDATE articles SET title_en=? WHERE id=?",
                             (a.get("title_en", ""), a["id"]))
            conn.commit()

        # ── STEP 0.5: FETCH ARTICLE BODIES ──
        if do_bodies:
            _fetch_bodies(conn, articles, progress)

        # ── STEP 0.6: SEMANTIC FILTER (second-pass recall) ──
        # Scores keyword-FAILED articles against the risk-domain reference set
        # and promotes high-confidence matches into review. Runs on title_en, so
        # any filtered-out Bengali candidate is translated first.
        if do_semantic and not (should_stop and should_stop()):
            try:
                promoted = _run_semantic_stage(conn, today, progress)
            except Exception as e:
                # a semantic failure must NEVER stop dedup + scoring from running
                print(f"[pipeline] semantic stage failed (non-fatal): {e}")
                promoted = 0
            # promoted articles were just moved from filtered_out → pending; pull
            # them into the working set so they flow through dedup + judge too.
            if promoted:
                new_rows = conn.execute(
                    """SELECT id, url, source, title, title_en, sections, keywords,
                              dup_group, is_primary, also_in, content_text
                       FROM articles
                       WHERE day=? AND review_status='pending'
                             AND match_type='semantic'
                             AND llm_score IS NULL""",
                    (today,)).fetchall()
                existing_ids = {a["id"] for a in articles}
                for r in new_rows:
                    if r["id"] not in existing_ids:
                        articles.append(dict(r))
                total = len(articles)

        # ── STEP 1: DEDUP = embedding similarity + LLM verification ──
        if do_dedup and not (should_stop and should_stop()):
            # Belt-and-suspenders: make sure EVERY article entering dedup has an
            # English title. Translation failures earlier (LLM hiccup) or a
            # standalone dedup run could otherwise leave raw Bengali titles,
            # which bge-m3 wrongly merges across the language line. Translate any
            # stragglers now so dedup only ever sees English.
            missing_tr = [a for a in articles
                          if not (a.get("title_en") or "").strip()
                          and any("\u0980" <= ch <= "\u09FF" for ch in (a.get("title") or ""))]
            if missing_tr:
                print(f"  [pipeline] translating {len(missing_tr)} stragglers before dedup")
                fixed = stage_translate(missing_tr, enabled=True)
                for a in fixed:
                    conn.execute("UPDATE articles SET title_en=? WHERE id=?",
                                 (a.get("title_en", ""), a["id"]))
                conn.commit()
            if progress:
                progress(0, total, "Grouping duplicates")
            articles = stage_dedup(articles, enabled=True)
            articles = stage_llm_verify_groups(articles, enabled=True)
            for a in articles:
                conn.execute(
                    "UPDATE articles SET dup_group=?, is_primary=?, also_in=? WHERE id=?",
                    (a.get("dup_group"), 1 if a.get("is_primary", 1) else 0,
                     a.get("also_in", ""), a["id"]))
            conn.commit()
            if progress:
                progress(total, total, "Grouping duplicates")

        # ── STEP 2: RELEVANCE — score in BATCHES ──
        if do_judge and not (should_stop and should_stop()):
            BATCH = int(os.environ.get("LLM_BATCH", "10"))
            scored = 0
            for start in range(0, len(articles), BATCH):
                if should_stop and should_stop():
                    break        # Force Stop — keep scores already written
                chunk = articles[start:start + BATCH]
                judgments = provider.judge_batch(chunk)
                for a, j in zip(chunk, judgments):
                    a["llm_score"] = j.score
                    a["llm_reason"] = j.reason
                    conn.execute(
                        "UPDATE articles SET llm_score=?, llm_reason=? WHERE id=?",
                        (j.score, j.reason, a["id"]))
                conn.commit()
                scored += len(chunk)
                if progress:
                    progress(scored, len(articles), "Scoring relevance")

        # ── STEP 3: TOPIC CLUSTERING (view-only grouping aid) ──
        # Groups today's scored primaries into broad TOPICS (broader than dedup's
        # same-event groups) so the review page can collapse a busy theme behind
        # a "+N more on this topic" bar. Stores a label in topic_tag. Reads the
        # scored set FRESH from the DB (not `articles`, whose scope varies by
        # stage) so it works on a full run AND as a standalone re-run. Fully
        # isolated: any failure leaves topic_tag empty and the review page falls
        # back to flat rendering — it can never block or corrupt the pipeline.
        if do_topic and not (should_stop and should_stop()):
            try:
                # cluster only dedup PRIMARIES scoring >= 5 (the ones that can
                # reach the top of the review list); duplicates hang off their
                # primary and don't need a topic of their own.
                prim = conn.execute(
                    """SELECT id, title, title_en, llm_score, source, content_text
                       FROM articles
                       WHERE day=? AND review_status='pending'
                         AND COALESCE(is_primary,1)=1
                         AND llm_score IS NOT NULL AND llm_score >= 5
                       ORDER BY llm_score DESC, published DESC""",
                    (today,)).fetchall()
                prim = [dict(r) for r in prim]

                if len(prim) >= 2 and hasattr(provider, "cluster_topics_chunk"):
                    # ── Phase 1: sequential chunked labelling with a running
                    # canonical-label list, so the same topic doesn't fragment
                    # across chunks and the label space stays bounded even at
                    # 700-800 articles. ──
                    CHUNK = int(os.environ.get("LLM_TOPIC_CHUNK", "50"))
                    labels_by_id = {}
                    known = []        # canonical labels established so far
                    known_lc = set()  # normalized set for dedup
                    import re as _re
                    def _norm_label(s):
                        # lowercase, drop punctuation, collapse whitespace so
                        # 'Middle East / oil' == 'middle east oil' == 'Middle East/Oil'
                        s = _re.sub(r"[^\w\s]", " ", (s or "").lower())
                        return _re.sub(r"\s+", " ", s).strip()
                    n_chunks = (len(prim) + CHUNK - 1) // CHUNK
                    for ci in range(n_chunks):
                        if should_stop and should_stop():
                            break
                        chunk = prim[ci * CHUNK:(ci + 1) * CHUNK]
                        if progress:
                            progress(ci, n_chunks + 1, "Grouping topics")
                        labs = provider.cluster_topics_chunk(chunk, known)
                        for a, lab in zip(chunk, labs):
                            lab = (lab or "").strip()
                            nl = _norm_label(lab)
                            labels_by_id[a["id"]] = lab
                            if lab and nl not in known_lc:
                                known.append(lab)
                                known_lc.add(nl)

                    # persist labels; clear any stale standout (recomputed below)
                    n_tagged = 0
                    for a in prim:
                        lab = labels_by_id.get(a["id"], "")
                        conn.execute(
                            "UPDATE articles SET topic_tag=?, topic_standout=0, "
                            "topic_parent_id=NULL WHERE id=?",
                            (lab, a["id"]))
                        if lab:
                            n_tagged += 1
                    conn.commit()

                    # ── CONSOLIDATION: merge duplicate topic labels of the SAME
                    # theme BEFORE folding, so cross-fragment duplicates (e.g. a
                    # Hormuz story stranded under 'Maritime security' vs the one
                    # under 'Middle East conflict') land in one topic and can
                    # actually fold. Two steps: (1) normalize-merge exact/near
                    # duplicates; (2) one LLM call to merge synonym labels. ──
                    distinct = sorted({l for l in labels_by_id.values() if l})
                    # step 1: normalize-collapse — pick a canonical surface form
                    # per normalized key (first seen), remap the rest to it
                    norm_canon = {}
                    for l in distinct:
                        k = _norm_label(l)
                        norm_canon.setdefault(k, l)
                    label_map = {l: norm_canon[_norm_label(l)] for l in distinct}
                    # step 2: LLM synonym-merge on the (smaller) canonical set
                    canon_set = sorted(set(label_map.values()))
                    if hasattr(provider, "consolidate_labels") and len(canon_set) > 1:
                        merged = provider.consolidate_labels(canon_set)
                        # compose: original -> norm-canon -> llm-canon
                        label_map = {l: merged.get(label_map[l], label_map[l])
                                     for l in distinct}
                    # apply the remap to every article
                    if label_map:
                        for aid, lab in list(labels_by_id.items()):
                            if lab in label_map:
                                labels_by_id[aid] = label_map[lab]
                        # rewrite persisted topic_tag to the consolidated label
                        for a in prim:
                            lab = labels_by_id.get(a["id"], "")
                            conn.execute("UPDATE articles SET topic_tag=? WHERE id=?",
                                         (lab, a["id"]))
                        conn.commit()
                        n_before = len(distinct)
                        n_after = len({l for l in labels_by_id.values() if l})
                        print(f"  [topic] consolidated {n_before} → {n_after} topics")

                    # ── Phase 2/3: per-topic standout flagging. Group the tagged
                    # primaries by label; for each topic with 2+ events make one
                    # small batched de-dup where the LLM only answers 'is this a
                    # restatement of an already-shown story' — Python drives the
                    # batching and parent bookkeeping. ──
                    from collections import defaultdict
                    by_topic = defaultdict(list)
                    for a in prim:
                        lab = labels_by_id.get(a["id"], "")
                        if lab:
                            by_topic[lab].append(a)

                    BATCH = int(os.environ.get("LLM_TOPIC_DEDUP_BATCH", "8"))
                    n_substantial = n_folded = 0
                    if hasattr(provider, "find_duplicates_batch"):
                        # biggest topics first (most folding to be gained there)
                        for lab, members in sorted(by_topic.items(),
                                                   key=lambda kv: -len(kv[1])):
                            if should_stop and should_stop():
                                break
                            if len(members) < 2:
                                # lone-topic article is trivially its own card
                                conn.execute(
                                    "UPDATE articles SET topic_standout=1, "
                                    "topic_parent_id=NULL WHERE id=?",
                                    (members[0]["id"],))
                                n_substantial += 1
                                continue
                            # members already in score-desc order (prim sorted),
                            # so the highest-scored of any duplicate set tends to
                            # be SHOWN first and become the representative.
                            # ── embedding pre-fold: collapse near-identical
                            # headlines deterministically (catches obvious dupes,
                            # esp. body-less ones, without an LLM call) ──
                            pf_thresh = float(os.environ.get("TOPIC_PREFOLD_THRESH", "0.82"))
                            members, pf_map = _embed_prefold(members, pf_thresh)
                            for fid, lead_id in pf_map.items():
                                conn.execute(
                                    "UPDATE articles SET topic_standout=0, "
                                    "topic_parent_id=? WHERE id=?",
                                    (lead_id, fid))
                                n_folded += 1
                            if len(members) < 2:
                                # everything collapsed to one lead by pre-fold
                                conn.execute(
                                    "UPDATE articles SET topic_standout=1, "
                                    "topic_parent_id=NULL WHERE id=?",
                                    (members[0]["id"],))
                                n_substantial += 1
                                continue
                            shown = []          # list of member dicts kept as cards
                            for start in range(0, len(members), BATCH):
                                if should_stop and should_stop():
                                    break
                                batch = members[start:start + BATCH]
                                # ── step 1: fold candidates that restate an
                                # already-shown card ──
                                dupes = provider.find_duplicates_batch(shown, batch)
                                survivors = []
                                for ci, m in enumerate(batch):
                                    si = dupes.get(ci)
                                    if si is not None and 0 <= si < len(shown):
                                        conn.execute(
                                            "UPDATE articles SET topic_standout=0, "
                                            "topic_parent_id=? WHERE id=?",
                                            (shown[si]["id"], m["id"]))
                                        n_folded += 1
                                    else:
                                        survivors.append(m)
                                # ── step 2: dedupe the batch's survivors among
                                # THEMSELVES (score-sorting puts same-story
                                # articles adjacent → same batch, so without this
                                # pass intra-batch duplicates all auto-promote).
                                # Iterative: promote the first remaining survivor
                                # as lead, fold its restatements, repeat with the
                                # leftovers. Each call's reference is exactly ONE
                                # lead, so the model's indices can't drift; calls
                                # per batch = number of distinct stories (≤8). ──
                                remaining = survivors
                                while remaining:
                                    leadm = remaining[0]
                                    conn.execute(
                                        "UPDATE articles SET topic_standout=1, "
                                        "topic_parent_id=NULL WHERE id=?",
                                        (leadm["id"],))
                                    shown.append(leadm)
                                    n_substantial += 1
                                    rest = remaining[1:]
                                    if not rest:
                                        break
                                    d2 = provider.find_duplicates_batch([leadm], rest)
                                    nxt = []
                                    for cj, m in enumerate(rest):
                                        if d2.get(cj) == 0:      # dupe of the lead
                                            conn.execute(
                                                "UPDATE articles SET topic_standout=0, "
                                                "topic_parent_id=? WHERE id=?",
                                                (leadm["id"], m["id"]))
                                            n_folded += 1
                                        else:
                                            nxt.append(m)
                                    remaining = nxt
                        conn.commit()
                        conn.commit()

                    if progress:
                        progress(n_chunks + 1, n_chunks + 1, "Grouping topics")
                    print(f"  [topic] tagged {n_tagged} of {len(prim)} scored "
                          f"primaries into {len(by_topic)} topics; "
                          f"{n_substantial} substantial cards, {n_folded} folded")
                elif len(prim) < 2:
                    print(f"  [topic] only {len(prim)} scored primaries — nothing to group")
            except Exception as e:
                print(f"  [topic] skipped (non-fatal): {e}")

        return {"enriched": total}
    finally:
        conn.close()


def _embed_prefold(members, thresh=0.82):
    """Deterministic pre-fold within one topic using bge-m3 headline embeddings.
    Folds near-identical headlines (cosine >= thresh) into the highest-scored
    member of each similarity cluster — catches obvious same-story pairs (esp.
    when body text is missing) without an LLM call. `members` must be in
    score-desc order so cluster leads are the highest-scored.

    Returns (leads, fold_map):
      leads    = members that remain (cluster representatives + singletons)
      fold_map = {member_id: lead_id} for folded members
    On any failure returns (members, {}) — nothing pre-folded, LLM handles all.
    """
    try:
        import dedup as _dedup
        if not _dedup.is_available() or len(members) < 2:
            return members, {}
        import numpy as np
        model = _dedup._get_model()
        titles = [_dedup._prep((m.get("title_en") or m.get("title") or "").strip())
                  for m in members]
        emb = model.encode(titles, normalize_embeddings=True,
                           show_progress_bar=False)
        sims = emb @ emb.T
        n = len(members)
        assigned = [False] * n     # already folded into an earlier lead
        leads, fold_map = [], {}
        for i in range(n):
            if assigned[i]:
                continue
            leads.append(members[i])          # i is a cluster lead (highest score)
            for j in range(i + 1, n):
                if not assigned[j] and sims[i, j] >= thresh:
                    assigned[j] = True
                    fold_map[members[j]["id"]] = members[i]["id"]
        return leads, fold_map
    except Exception as e:
        if os.environ.get("TOPIC_DEBUG", "1") != "0":
            print(f"    [prefold] skipped ({type(e).__name__}: {e})")
        return members, {}


def _fetch_bodies(conn, articles, progress=None):
    """Fetch article body text for keyword-matched pending articles that don't
    have content_text yet. Uses concurrent workers for speed. Stores the text
    in the DB so it's available for relevance scoring and report generation.

    Only fetches for articles that passed the keyword filter (review_status=pending,
    not filtered_out) — typically ~500-700 articles, not the full 4-5k collected.
    """
    from concurrent.futures import ThreadPoolExecutor
    import article_fetch

    to_fetch = [a for a in articles
                if not a.get("content_text")]
    if not to_fetch:
        return

    total = len(to_fetch)
    print(f"[bodies] Fetching article bodies for {total} articles...")
    if progress:
        progress(0, total, "Fetching article bodies")

    def _fetch_one(article):
        url = article.get("url", "")
        text = article_fetch.fetch_article_text(url)
        return article["id"], text

    done = 0
    BATCH = 20
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i in range(0, total, BATCH):
            chunk = to_fetch[i:i + BATCH]
            for article_id, text in pool.map(_fetch_one, chunk):
                if text:
                    conn.execute("UPDATE articles SET content_text=? WHERE id=?",
                                 (text, article_id))
                    # also update the in-memory article dict
                    for a in articles:
                        if a["id"] == article_id:
                            a["content_text"] = text
                            break
            conn.commit()
            done += len(chunk)
            if progress:
                progress(done, total, "Fetching article bodies")

    fetched = sum(1 for a in articles if a.get("content_text"))
    print(f"[bodies] Done: {fetched}/{total} bodies fetched successfully")


def _run_semantic_stage(conn, today, progress=None):
    """Semantic second-pass filter. Considers keyword-FAILED articles that
    already have an English title, scores them against the risk-domain
    reference set, and PROMOTES high-confidence matches to
    review_status='pending' with match_type='semantic'. Keyword-matched
    articles are stamped match_type='keyword' so the two pools are queryable.
    """
    import os
    import semantic

    if progress:
        progress(0, 1, "Semantic filtering")

    # 1. stamp keyword pool
    conn.execute(
        "UPDATE articles SET match_type='keyword' "
        "WHERE day=? AND review_status='pending' AND sections!='' "
        "AND (match_type IS NULL OR match_type='')",
        (today,))
    conn.commit()

    # 2. gather ALL keyword-failed candidates for today. The model is bilingual,
    #    so we score each article on its NATIVE title — Bengali articles on their
    #    Bengali title, English on English. No pre-translation needed here (that
    #    happens later, before dedup). Reference set carries both EN + BN keyword
    #    phrases so both sides match in-language.
    rows = conn.execute(
        """SELECT id, url, source, title, title_en, sections, keywords
           FROM articles
           WHERE day=? AND review_status='filtered_out'""",
        (today,)).fetchall()
    candidates = [dict(r) for r in rows]
    if not candidates:
        print("  [semantic] no filtered-out candidates today")
        return 0
    # safety cap so an enormous filtered pool can't blow up embedding time
    MAX_CAND = int(os.environ.get("SEMANTIC_MAX_CANDIDATES", "6000"))
    if len(candidates) > MAX_CAND:
        candidates = candidates[:MAX_CAND]

    # 4. keyword rows for the reference set
    kw_rows = conn.execute(
        "SELECT term_en, term_bn, section FROM keywords WHERE enabled=1"
    ).fetchall()
    kw_tuples = [(r["term_en"], r["term_bn"], r["section"]) for r in kw_rows]

    # 5. score candidates (semantic.run_semantic_filter marks match_type on the
    #    dicts and returns how many crossed the threshold)
    for a in candidates:
        a["sections"] = ""          # ensure treated as keyword-failed
    n_new = semantic.run_semantic_filter(conn, candidates, kw_tuples,
                                         progress=progress)

    # 6. promote semantic matches into review
    promoted = 0
    for a in candidates:
        if a.get("match_type") == "semantic":
            conn.execute(
                "UPDATE articles SET review_status='pending', match_type='semantic', "
                "semantic_score=? WHERE id=?",
                (a.get("semantic_score"), a["id"]))
            promoted += 1
    conn.commit()
    print(f"  [semantic] promoted {promoted} articles into review")
    if progress:
        progress(1, 1, "Semantic filtering")
    return promoted

"""The P3 stage orchestrator — the faithful successor to
`collection_service.run_llm_enrichment()` (`collection_service.py:229-620`),
**NOT** `pipeline.enrich()`. Per the mission's fact (a) and
`docs/architecture-decision-record.md` §6: `pipeline.enrich()` is
imported once (`collection_service.py:23`) and never called; it reads
like the P3 specification and is not the code that runs. Everything
below mirrors `run_llm_enrichment()`'s actual inline sequence, not
`pipeline.py`'s.

Stage sequence (`docs/p3-contract.md` §6), each independently gated:

    STEP 0  translate   — non-English titles -> title_en
    STEP 1  dedup       — embedding pre-cluster (unavailable, see
                           `app.services.llm.embedding`) + LLM
                           `verify_group()` split pass
    STEP 2  judge       — `judge_batch()` in `LLM_BATCH`-sized chunks
    STEP 3  topic       — `cluster_topics_chunk` -> `consolidate_labels`
                           -> per-topic `find_duplicates_batch` fold pass

**Deliberately NOT ported**, per explicit scope boundaries:

  * STEP 0.5 (body fetching, `_fetch_bodies`) — a network-fetch concern
    that legacy already runs OFF by default even on a full run
    (`do_bodies` is `False` whenever `stages is None`, i.e. every normal
    daily run); this module accepts a `"bodies"` stage name for
    row-selection-precedence FIDELITY (see `_select_where` below) but
    does not fetch anything — that belongs with P1-INGEST's collector,
    which this task must not touch.
  * STEP 0.6 (semantic second-pass recall) — REMOVED per ADR §1/§6:
    "4 of 51,886 rows ever promoted... stripping it is overdue." No
    `do_semantic` flag, no semantic row-selection branch exists here at
    all; `docs/p3-contract.md` §6.1's own precedence chain already
    flags this stage as "[being removed]".

Routing is entirely config-driven (`app.services.llm.routing` /
`app.services.llm.registry` — DoD #1): each stage below resolves its
OWN `LLMClient` via `build_client_for_feature()`, so translation,
dedup-verification, scoring, and topic-clustering can each be served by
a different provider without any code change here. A feature with no
configured provider (no admin-entered key, no bootstrap env key)
degrades to a per-stage no-op — logged, never a crash — mirroring
legacy's own "no article ever silently dropped" philosophy
(`docs/p3-contract.md` §7) one level up: an unconfigured STAGE now
behaves the way a FAILED CALL always did (fall through to the
neutral/untranslated/untagged default), rather than raising.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.db.connection import DictConnection
from app.errors import ConfigurationError
from app.logging import get_logger
from app.services.llm import parsing
from app.services.llm.concurrency import limiter_for, parallel_map, provider_of
from app.services.llm.embedding import EmbeddingBackend, default_embedding_backend
from app.services.llm.provider import LLMClient
from app.services.llm.registry import build_client_for_feature
from app.services.llm.routing import DEDUP_VERIFICATION, SCORING, TOPIC_CLUSTERING, TRANSLATION
from app.services.matching import is_bengali
from app.services.near_duplicates import propose_duplicate_groups

logger = get_logger(__name__)

ProgressCallback = Callable[[int, int, str], None]
ShouldStopCallback = Callable[[], bool]

# `content_text` is NOT a column on `articles` in this schema — it lives
# in the 1:1 child table `article_bodies` (migrations/0004, split out
# specifically because it's the single heaviest field; see that
# migration's own header comment). Legacy's `articles.content_text` is
# therefore `a.id, ..., COALESCE(ab.content_text, '') AS content_text`
# via a LEFT JOIN here — every SELECT against `articles` that legacy
# expects a `content_text` column on must join `article_bodies` the
# same way (see `_run_topic_clustering` below, which needs it too).
ARTICLE_SELECT_COLUMNS = (
    "a.id, a.url, a.source, a.title, a.title_en, a.sections, a.keywords, a.dup_group, "
    "a.is_primary, a.also_in, COALESCE(ab.content_text, '') AS content_text"
)
ARTICLE_SELECT_FROM = "articles a LEFT JOIN article_bodies ab ON ab.article_id = a.id"

STAGE_TRANSLATE = "translate"
STAGE_DEDUP = "dedup"
STAGE_JUDGE = "judge"
STAGE_TOPIC = "topic"
STAGE_BODIES = "bodies"  # accepted for row-selection fidelity only — see module docstring
ALL_STAGES = (STAGE_TRANSLATE, STAGE_DEDUP, STAGE_JUDGE, STAGE_TOPIC)

TOPIC_PREFOLD_DEFAULT_THRESHOLD = 0.82  # p3-contract.md §6.3 — TOPIC_PREFOLD_THRESH env default

# How many articles the translate stage completes before committing.
# Small enough that progress is visible and a crash loses seconds of
# work rather than the whole stage; large enough that the thread pool
# still has a full batch to parallelise across (see the persistence
# comment in `run_llm_enrichment`).
TRANSLATE_PERSIST_CHUNK = 50


@dataclass
class EnrichmentResult:
    enriched: int
    skipped_stages: tuple[str, ...] = field(default_factory=tuple)


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default) != "0"


def _looks_english(text: str) -> bool:
    """`pipeline.py:228-234` `_looks_english()`, verbatim: ASCII-char
    ratio > 0.6 — used to prefer an English headline as a dedup group's
    representative.
    """
    if not text:
        return False
    ascii_n = sum(1 for ch in text if ord(ch) < 128)
    return ascii_n / max(len(text), 1) > 0.6


def _try_build_client(conn: DictConnection, feature: str) -> LLMClient | None:
    """Never raises: a feature with no configured provider degrades to
    `None`, and every stage below treats `None` the way legacy treats
    `provider.enabled == False` for that stage — a documented no-op,
    not a crash.
    """
    try:
        return build_client_for_feature(conn, feature)
    except ConfigurationError as exc:
        logger.warning("llm_feature_unconfigured", extra={"feature": feature, "error": str(exc)})
        return None


# ── STEP 0: translate ───────────────────────────────────────────────────


def stage_translate(
    articles: list[dict[str, Any]], client: LLMClient | None, progress: ProgressCallback | None = None
) -> list[dict[str, Any]]:
    """`pipeline.py:238-276` `stage_translate()`, same per-article
    semantics. Already-translated articles are skipped (cached);
    already-English titles get `title_en = title` for free (no LLM
    call); everything else calls `client.translate_title()` if a client
    is configured, else falls back to the untranslated title — exactly
    legacy's `else` branch when `provider.enabled` is False.

    The calls that DO need the network are issued concurrently
    (`llm.concurrency.parallel_map`, rate-limited per provider) rather
    than one-at-a-time. This changes wall-clock time only: the prompt,
    the one-title-per-call shape (`BATCH_SIZE_TRANSLATE = 1`), and the
    per-article fallback are all untouched, and results are applied by
    input index so no title can land on the wrong article. Sequentially
    this stage was 173 serial round-trips on a real run — several
    minutes of pure socket wait for work that has no ordering
    constraint at all.
    """
    total = len(articles)

    # Partition first: everything resolvable without a network call is
    # settled here, so only genuine work reaches the thread pool.
    needs_call: list[dict[str, Any]] = []
    for a in articles:
        if a.get("title_en"):
            continue
        title = a.get("title", "")
        if _looks_english(title) or client is None:
            a["title_en"] = title
        else:
            needs_call.append(a)

    already_done = total - len(needs_call)
    if progress:
        progress(already_done, total, "Translating")

    if not needs_call:
        return articles

    assert client is not None  # guaranteed by the partition above

    def _translate(article: dict[str, Any]) -> str:
        return client.translate_title(article.get("title", ""))

    def _report(done: int, _n: int) -> None:
        if progress:
            progress(already_done + done, total, "Translating")

    outcomes = parallel_map(
        needs_call,
        _translate,
        limiter=limiter_for(provider_of(client)),
        label="translate",
        on_done=_report,
    )
    for article, (translated, error) in zip(needs_call, outcomes, strict=True):
        # Same neutral default as the sequential version: an empty or
        # failed translation leaves the original title in place rather
        # than blanking it (p3-contract.md §7 — never silently drop).
        if error is not None:
            article["title_en"] = article.get("title", "")
        else:
            article["title_en"] = translated or article.get("title", "")
    return articles


# ── STEP 1: dedup (embedding pre-cluster + LLM verify) ──────────────────


def _sequence_group_id_allocator(conn: DictConnection) -> Callable[[], int]:
    """Draw `dup_group` ids from `articles_dup_group_seq`
    (migrations/0017) so cluster ids are unique across every day and
    every concurrent run — see that migration's header for the
    cross-day sibling-mixing bug this prevents.
    """

    def allocate() -> int:
        row = conn.execute("SELECT nextval('articles_dup_group_seq') AS id").fetchone()
        assert row is not None
        return int(row["id"])

    return allocate


def counting_group_id_allocator(start: int = 1) -> Callable[[], int]:
    """In-memory allocator for tests and any caller without a DB
    connection. NOT for production use — it restarts at `start` on every
    call site, which is precisely the per-run numbering migrations/0017
    exists to eliminate.
    """
    counter = {"next": start}

    def allocate() -> int:
        value = counter["next"]
        counter["next"] += 1
        return value

    return allocate


def stage_dedup_embeddings(
    articles: list[dict[str, Any]],
    backend: EmbeddingBackend,
    *,
    allocate_id: Callable[[], int] | None = None,
) -> list[dict[str, Any]]:
    """Propose candidate duplicate groups — the first half of P3's
    two-pass dedup, whose second half is `stage_llm_verify_groups`.

    Originally this delegated entirely to an embedding backend and,
    when none was available, passed every article through with
    `dup_group = None`. Since `UnavailableEmbeddingBackend` is the only
    backend that has ever existed, that "fallback" was the ONLY path
    ever taken, and because the LLM verify pass skips `None` groups, the
    whole dedup stage has always been a no-op that still reported
    success. See `app.services.near_duplicates` for the full write-up.

    A real embedding backend, if one is ever added, still takes
    precedence — this lexical pass is the floor, not a replacement for
    it.
    """
    if not articles:
        return articles
    if backend.is_available():
        # Integration seam preserved for a future embedding backend
        # (see app.services.llm.embedding). Not reachable today.
        raise NotImplementedError("embedding backend reported available but no implementation exists")

    propose_duplicate_groups(articles, allocate_id=allocate_id or counting_group_id_allocator())
    for a in articles:
        a.setdefault("is_primary", True)
        a.setdefault("also_in", "")
    return articles


def stage_llm_verify_groups(
    articles: list[dict[str, Any]],
    client: LLMClient | None,
    *,
    allocate_id: Callable[[], int] | None = None,
) -> list[dict[str, Any]]:
    """`pipeline.py:164-225` `stage_llm_verify_groups()`, verbatim logic:
    for every embedding-proposed group with 2+ members, ask the LLM to
    SPLIT it (never merge across groups it wasn't given together),
    `CHUNK = 12` headlines per call, then recompute `is_primary`/
    `also_in` per (possibly new) group with an English-headline-first
    tiebreak.
    """
    if not articles or client is None:
        return articles

    groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for a in articles:
        groups[a.get("dup_group")].append(a)

    # Split-off subgroups must also draw from the global id space —
    # `max(existing) + 1` was per-run numbering with the same cross-day
    # collision hazard migrations/0017 documents.
    allocate = allocate_id or counting_group_id_allocator(
        max((g for g in groups if isinstance(g, int)), default=0) + 1
    )

    chunk_size = parsing.BATCH_SIZE_DEDUP_VERIFY
    for gid, members in groups.items():
        if gid is None or len(members) < 2:
            continue
        member_chunks = [members[i : i + chunk_size] for i in range(0, len(members), chunk_size)]
        for chunk in member_chunks:
            if len(chunk) < 2:
                continue
            headlines = [(m.get("title_en") or m.get("title", "")) for m in chunk]
            sub = client.verify_group(headlines)
            subgroup_to_gid: dict[int, int] = {}
            for m, s in zip(chunk, sub, strict=False):
                if s not in subgroup_to_gid:
                    subgroup_to_gid[s] = allocate()
                m["dup_group"] = subgroup_to_gid[s]

    regrouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for a in articles:
        gid = a.get("dup_group")
        if gid is None:
            a["is_primary"] = True
            a["also_in"] = ""
            continue
        regrouped[gid].append(a)
    for _gid, members in regrouped.items():
        members.sort(key=lambda m: (0 if _looks_english(m.get("title", "")) else 1))
        for i, m in enumerate(members):
            m["is_primary"] = i == 0
        others = [m.get("source", "") for m in members[1:]]
        members[0]["also_in"] = ", ".join(others)
        for m in members[1:]:
            m["also_in"] = ""
    return articles


# ── STEP 3: topic clustering helpers ─────────────────────────────────────


def _norm_label(s: str) -> str:
    """collection_service.py's `_norm_label()`: lowercase, drop
    punctuation, collapse whitespace so 'Middle East / oil' ==
    'middle east oil' == 'Middle East/Oil'.
    """
    normalized = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", normalized).strip()


def embed_prefold(
    members: list[dict[str, Any]], thresh: float, backend: EmbeddingBackend
) -> tuple[list[dict[str, Any]], dict[Any, Any]]:
    """`collection_service.py:623-661` `_embed_prefold()`, verbatim
    logic: on any failure (including "no backend available", the only
    path reachable today) returns `(members, {})` — nothing pre-folded,
    the LLM `find_duplicates_batch` pass handles everything.
    """
    try:
        if not backend.is_available() or len(members) < 2:
            return members, {}
        raise NotImplementedError("embedding backend reported available but no implementation exists")
    except Exception as exc:  # noqa: BLE001 - mirrors legacy's own broad except here
        logger.warning("topic_prefold_failed_nonfatal", extra={"error": str(exc)})
        return members, {}


# ── the orchestrator ──────────────────────────────────────────────────────


def _select_where(
    *, full_run: bool, do_bodies: bool, do_dedup: bool, do_judge: bool, do_translate: bool, do_topic: bool
) -> str:
    """`collection_service.py:274-298`'s exact precedence chain, adapted
    to Postgres `%s` placeholders. The legacy chain's 4th branch also
    checked `not do_semantic`; that flag no longer exists at all (ADR:
    semantic search removed — see module docstring), so it's dropped
    from the condition rather than hardcoded to a permanently-true term.
    """
    if not full_run and do_bodies and not do_dedup and not do_judge and not do_translate:
        return "day=%s AND review_status='pending' AND (ab.content_text IS NULL OR ab.content_text='')"
    if not full_run and do_dedup and not do_judge and not do_translate:
        return "day=%s AND review_status='pending'"
    if not full_run and do_translate and not do_dedup and not do_judge:
        return "day=%s AND review_status='pending' AND (title_en IS NULL OR title_en='')"
    if not full_run and do_topic and not do_dedup and not do_judge and not do_translate:
        return "day=%s AND review_status='pending'"
    return "day=%s AND review_status='pending' AND llm_score IS NULL"


def run_llm_enrichment(
    conn: DictConnection,
    *,
    today: date | None = None,
    stages: set[str] | None = None,
    progress: ProgressCallback | None = None,
    should_stop: ShouldStopCallback | None = None,
    embedding_backend: EmbeddingBackend | None = None,
) -> EnrichmentResult:
    """Run the P3 pipeline over `today`'s pending articles.

    `stages=None` (the default) is a FULL RUN: which of translate/dedup/
    judge/topic run is driven by `LLM_TRANSLATE`/`LLM_DEDUP`/`LLM_JUDGE`/
    `LLM_TOPIC` env vars (default ON for all four), matching
    `collection_service.py:257-264`. Pass a set like `{"translate"}` or
    `{"dedup"}` for a standalone stage re-run (p3-contract.md §6) — row
    selection differs per mode, see `_select_where()`.

    Commits incrementally after each stage's writes (mirrors legacy:
    "Updates the DB as it goes so the review UI shows results filling in
    live") — callers should pass an already-open connection they manage
    the lifetime of, not one from a `with get_connection()` block that
    also wants to commit/rollback the whole run atomically.
    """
    today_val = today or date.today()
    embedding_backend = embedding_backend or default_embedding_backend()

    full_run = stages is None
    want = set(stages) if stages else set()
    do_translate = (STAGE_TRANSLATE in want) if not full_run else _env_flag("LLM_TRANSLATE")
    do_dedup = (STAGE_DEDUP in want) if not full_run else _env_flag("LLM_DEDUP")
    do_judge = (STAGE_JUDGE in want) if not full_run else _env_flag("LLM_JUDGE")
    do_bodies = (STAGE_BODIES in want) if not full_run else False
    do_topic = (STAGE_TOPIC in want) if not full_run else _env_flag("LLM_TOPIC")

    if do_bodies:
        logger.info(
            "llm_bodies_stage_out_of_scope", extra={"detail": "body fetching is owned by P1-INGEST, not P3-LLM"}
        )

    where = _select_where(
        full_run=full_run,
        do_bodies=do_bodies,
        do_dedup=do_dedup,
        do_judge=do_judge,
        do_translate=do_translate,
        do_topic=do_topic,
    )
    rows = conn.execute(
        f"SELECT {ARTICLE_SELECT_COLUMNS} FROM {ARTICLE_SELECT_FROM} WHERE {where}", (today_val,)
    ).fetchall()
    articles = [dict(r) for r in rows]
    total = len(articles)
    if not total and not do_topic:
        return EnrichmentResult(enriched=0)

    skipped_stages: list[str] = []

    # ── STEP 0: translate ──
    if do_translate and not (should_stop and should_stop()):
        translate_client = _try_build_client(conn, TRANSLATION)
        if translate_client is None:
            skipped_stages.append(STAGE_TRANSLATE)

        # Persisted in CHUNKS, not once at the end. Translating a full
        # day is minutes of work (measured: ~77 titles/min against
        # Mistral, so a 1,376-article backlog day is ~18 minutes), and
        # writing only after the last one meant the whole stage was
        # invisible in the DB and in the UI until it finished, and a
        # crash or restart anywhere in that window threw away every
        # completed translation. Chunking makes the work durable as it
        # lands and lets a re-run skip what is already done — the
        # `if a.get("title_en")` cache check in `stage_translate` turns
        # already-persisted rows into no-ops.
        done_before = 0

        def _chunk_progress(done: int, total: int, label: str) -> None:
            if progress:
                progress(done_before + done, len(articles), label)

        for start in range(0, len(articles), TRANSLATE_PERSIST_CHUNK):
            if should_stop and should_stop():
                break
            chunk = articles[start : start + TRANSLATE_PERSIST_CHUNK]
            stage_translate(chunk, translate_client, progress=_chunk_progress)
            for a in chunk:
                conn.execute("UPDATE articles SET title_en=%s WHERE id=%s", (a.get("title_en", ""), a["id"]))
            conn.commit()
            done_before += len(chunk)
        if progress:
            progress(done_before, len(articles), "Translating")

    # ── STEP 1: dedup = embedding pre-cluster + LLM verify ──
    if do_dedup and not (should_stop and should_stop()):
        dedup_verify_client = _try_build_client(conn, DEDUP_VERIFICATION)
        if dedup_verify_client is None:
            skipped_stages.append(STAGE_DEDUP)

        # Straggler-translate safety net (collection_service.py:352-367):
        # any article still missing title_en AND containing Bengali
        # script gets force-translated right before dedup, even on a
        # standalone dedup rerun — bge-m3 (or any embedding model) wrongly
        # merges across the language line otherwise.
        missing_tr = [a for a in articles if not (a.get("title_en") or "").strip() and is_bengali(a.get("title") or "")]
        if missing_tr:
            straggler_client = _try_build_client(conn, TRANSLATION)
            stage_translate(missing_tr, straggler_client)
            for a in missing_tr:
                conn.execute("UPDATE articles SET title_en=%s WHERE id=%s", (a.get("title_en", ""), a["id"]))
            conn.commit()

        if progress:
            progress(0, total, "Grouping duplicates")
        allocate_group_id = _sequence_group_id_allocator(conn)
        articles = stage_dedup_embeddings(articles, embedding_backend, allocate_id=allocate_group_id)
        articles = stage_llm_verify_groups(articles, dedup_verify_client, allocate_id=allocate_group_id)
        for a in articles:
            conn.execute(
                "UPDATE articles SET dup_group=%s, is_primary=%s, also_in=%s WHERE id=%s",
                (a.get("dup_group"), bool(a.get("is_primary", True)), a.get("also_in", ""), a["id"]),
            )
        conn.commit()
        if progress:
            progress(total, total, "Grouping duplicates")

    # ── STEP 2: judge — score in BATCHES ──
    if do_judge and not (should_stop and should_stop()):
        judge_client = _try_build_client(conn, SCORING)
        if judge_client is None:
            skipped_stages.append(STAGE_JUDGE)
        else:
            batch_size = int(os.environ.get("LLM_BATCH", str(parsing.BATCH_SIZE_JUDGE_DEFAULT)))
            chunks = [articles[s : s + batch_size] for s in range(0, len(articles), batch_size)]

            # Batches are issued concurrently; DB writes stay on THIS
            # thread. `conn` is a single psycopg connection and is not
            # thread-safe, so the split is deliberate: parallelise the
            # network wait, never the cursor. Batch composition and the
            # per-batch prompt are unchanged from the sequential
            # version, and `strict=True` below pins each judgment set to
            # the batch that produced it.
            scored = 0

            def _report(done: int, n: int) -> None:
                if progress:
                    # Report in articles, not batches, so the UI number
                    # means the same thing it did before.
                    progress(min(done * batch_size, len(articles)), len(articles), "Scoring relevance")

            outcomes = parallel_map(
                chunks,
                judge_client.judge_batch,
                limiter=limiter_for(provider_of(judge_client)),
                label="judge",
                on_done=_report,
            )
            for chunk, (judgments, error) in zip(chunks, outcomes, strict=True):
                if should_stop and should_stop():
                    break
                if error is not None or judgments is None:
                    # `judge_batch` already converts provider errors into
                    # neutral per-item fallbacks internally; reaching here
                    # means something outside that contract failed. Leave
                    # the batch unscored rather than writing a wrong score
                    # — an unscored row stays visible as pending.
                    logger.warning("judge_batch_failed_leaving_unscored", extra={"batch_size": len(chunk)})
                    continue
                for a, j in zip(chunk, judgments, strict=False):
                    a["llm_score"] = j.score
                    a["llm_reason"] = j.reason
                    conn.execute(
                        "UPDATE articles SET llm_score=%s, llm_reason=%s WHERE id=%s", (j.score, j.reason, a["id"])
                    )
                conn.commit()
                scored += len(chunk)
            if progress:
                progress(scored, len(articles), "Scoring relevance")

    # ── STEP 3: topic clustering ──
    if do_topic and not (should_stop and should_stop()):
        topic_client = _try_build_client(conn, TOPIC_CLUSTERING)
        if topic_client is None:
            skipped_stages.append(STAGE_TOPIC)
        else:
            try:
                _run_topic_clustering(
                    conn,
                    today_val,
                    topic_client,
                    embedding_backend,
                    progress=progress,
                    should_stop=should_stop,
                )
            except Exception as exc:  # noqa: BLE001 - mirrors collection_service.py:615-616
                logger.warning("topic_clustering_skipped_nonfatal", extra={"error": str(exc)})

    return EnrichmentResult(enriched=total, skipped_stages=tuple(skipped_stages))


def _mark_standout(conn: DictConnection, article_id: Any) -> None:
    conn.execute("UPDATE articles SET topic_standout=TRUE, topic_parent_id=NULL WHERE id=%s", (article_id,))


def _mark_folded(conn: DictConnection, article_id: Any, lead_id: Any) -> None:
    conn.execute(
        "UPDATE articles SET topic_standout=FALSE, topic_parent_id=%s WHERE id=%s", (lead_id, article_id)
    )


def _run_topic_clustering(
    conn: DictConnection,
    today_val: date,
    client: LLMClient,
    embedding_backend: EmbeddingBackend,
    *,
    progress: ProgressCallback | None,
    should_stop: ShouldStopCallback | None,
) -> None:
    """`collection_service.py:409-616`'s STEP 3 body, verbatim logic.
    Reads scored primaries FRESH from the DB (bypasses `articles`) so it
    works identically on a full run and a standalone `{"topic"}` rerun.
    """
    prim_rows = conn.execute(
        """SELECT a.id, a.title, a.title_en, a.llm_score, a.source, COALESCE(ab.content_text, '') AS content_text
           FROM articles a LEFT JOIN article_bodies ab ON ab.article_id = a.id
           WHERE a.day=%s AND a.review_status='pending'
             AND COALESCE(a.is_primary, TRUE) = TRUE
             AND a.llm_score IS NOT NULL AND a.llm_score >= 5
           ORDER BY a.llm_score DESC, a.published DESC""",
        (today_val,),
    ).fetchall()
    prim = [dict(r) for r in prim_rows]

    if len(prim) < 2:
        logger.info("topic_clustering_too_few_primaries", extra={"count": len(prim)})
        return

    # ── Phase 1: sequential chunked labelling ──
    chunk_size = int(os.environ.get("LLM_TOPIC_CHUNK", str(parsing.BATCH_SIZE_TOPIC_CHUNK_DEFAULT)))
    labels_by_id: dict[Any, str] = {}
    known: list[str] = []
    known_lc: set[str] = set()

    n_chunks = (len(prim) + chunk_size - 1) // chunk_size
    for ci in range(n_chunks):
        if should_stop and should_stop():
            break
        chunk = prim[ci * chunk_size : (ci + 1) * chunk_size]
        if progress:
            progress(ci, n_chunks + 1, "Grouping topics")
        labs = client.cluster_topics_chunk(chunk, known)
        for a, lab in zip(chunk, labs, strict=False):
            lab = (lab or "").strip()
            nl = _norm_label(lab)
            labels_by_id[a["id"]] = lab
            if lab and nl not in known_lc:
                known.append(lab)
                known_lc.add(nl)

    n_tagged = 0
    for a in prim:
        lab = labels_by_id.get(a["id"], "")
        conn.execute(
            "UPDATE articles SET topic_tag=%s, topic_standout=FALSE, topic_parent_id=NULL WHERE id=%s", (lab, a["id"])
        )
        if lab:
            n_tagged += 1
    conn.commit()

    # ── consolidation: normalize-collapse, then one LLM synonym-merge call ──
    distinct = sorted({lab for lab in labels_by_id.values() if lab})
    norm_canon: dict[str, str] = {}
    for lab in distinct:
        norm_canon.setdefault(_norm_label(lab), lab)
    label_map = {lab: norm_canon[_norm_label(lab)] for lab in distinct}
    canon_set = sorted(set(label_map.values()))
    if len(canon_set) > 1:
        merged = client.consolidate_labels(canon_set)
        label_map = {lab: merged.get(label_map[lab], label_map[lab]) for lab in distinct}
    if label_map:
        for aid, lab in list(labels_by_id.items()):
            if lab in label_map:
                labels_by_id[aid] = label_map[lab]
        for a in prim:
            lab = labels_by_id.get(a["id"], "")
            conn.execute("UPDATE articles SET topic_tag=%s WHERE id=%s", (lab, a["id"]))
        conn.commit()
        n_after = len({lab for lab in labels_by_id.values() if lab})
        logger.info("topic_labels_consolidated", extra={"before": len(distinct), "after": n_after})

    # ── Phase 2/3: per-topic standout flagging, biggest topics first ──
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in prim:
        lab = labels_by_id.get(a["id"], "")
        if lab:
            by_topic[lab].append(a)

    fold_batch_size = int(os.environ.get("LLM_TOPIC_DEDUP_BATCH", str(parsing.BATCH_SIZE_TOPIC_DEDUP_DEFAULT)))
    n_substantial = n_folded = 0
    for _lab, members in sorted(by_topic.items(), key=lambda kv: -len(kv[1])):
        if should_stop and should_stop():
            break
        if len(members) < 2:
            _mark_standout(conn, members[0]["id"])
            n_substantial += 1
            continue

        pf_thresh = float(os.environ.get("TOPIC_PREFOLD_THRESH", str(TOPIC_PREFOLD_DEFAULT_THRESHOLD)))
        members, pf_map = embed_prefold(members, pf_thresh, embedding_backend)
        for fid, lead_id in pf_map.items():
            _mark_folded(conn, fid, lead_id)
            n_folded += 1
        if len(members) < 2:
            _mark_standout(conn, members[0]["id"])
            n_substantial += 1
            continue

        shown: list[dict[str, Any]] = []
        for start in range(0, len(members), fold_batch_size):
            if should_stop and should_stop():
                break
            batch = members[start : start + fold_batch_size]
            dupes = client.find_duplicates_batch(shown, batch)
            survivors = []
            for ci, m in enumerate(batch):
                si = dupes.get(ci)
                if si is not None and 0 <= si < len(shown):
                    _mark_folded(conn, m["id"], shown[si]["id"])
                    n_folded += 1
                else:
                    survivors.append(m)

            remaining = survivors
            while remaining:
                leadm = remaining[0]
                _mark_standout(conn, leadm["id"])
                shown.append(leadm)
                n_substantial += 1
                rest = remaining[1:]
                if not rest:
                    break
                d2 = client.find_duplicates_batch([leadm], rest)
                nxt = []
                for cj, m in enumerate(rest):
                    if d2.get(cj) == 0:
                        _mark_folded(conn, m["id"], leadm["id"])
                        n_folded += 1
                    else:
                        nxt.append(m)
                remaining = nxt
    conn.commit()

    if progress:
        progress(n_chunks + 1, n_chunks + 1, "Grouping topics")
    logger.info(
        "topic_clustering_done",
        extra={
            "tagged": n_tagged,
            "of": len(prim),
            "topics": len(by_topic),
            "substantial": n_substantial,
            "folded": n_folded,
        },
    )

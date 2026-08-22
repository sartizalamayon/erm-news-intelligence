"""Self-service watchlist entities -- and the corpus preview that is the
only reason self-service is allowed to exist.

THE PROBLEM THIS MODULE IS THE ANSWER TO
────────────────────────────────────────
`app/db/watchlist.py` spends its docstring arguing that adding a watch
entity should be a code change, with measurements to back it: the bare
Bangla token রবি matched 222 headlines of which two were the operator
(the rest being রবিবার, Sunday, and রবিউল, a given name); তরঙ্গ
("spectrum") is a substring of অন্তরঙ্গ ("intimate") and dragged in
celebrity stories; নগদ is the ordinary word for cash and বিকাশ the
ordinary word for development, so each collects roughly twice as many
headlines as it should.

The reason those are not hypothetical is that Postgres gives us no
usable word boundary for Bangla. `\\y` works for Latin -- `\\yrobi\\y`
matches "Robi's" and not "Robiul" -- but `'রবিবার' ~ '\\yরবি\\y'` is
TRUE, so a Bangla term is always a substring search. A text field on an
admin screen is therefore fully capable of re-importing every one of
those failures, and a wrong number on a risk dashboard is worse than no
number, because nobody downstream is filtering it.

The owner asked for self-service anyway. So the guard rail moves rather
than disappearing:

  * `preview()` runs the proposed pattern against the REAL corpus and
    reports what it matches -- with a context guard and without one,
    both totals and a sample of each. The operator sees the রবিবার
    problem happen to their own term before they save it, which is
    exactly the review that used to happen in a code review.
  * Everything the operator types is escaped, capped and lowercased
    before it becomes a regex, and is bound as a query parameter rather
    than interpolated. See `_escape_term` and `_clean_terms`.
  * The context guard is not free text. It names one of the hand-tuned
    cue sets in `app/db/watchlist.py`; see `GUARDS`.

WHAT A CUSTOM ENTITY LOOKS LIKE
───────────────────────────────
At most two rules, mirroring how the code-defined entities are written:
one for the Latin terms (wrapped in `\\y` word boundaries, which are
correct there) and one for the Bangla terms (not wrapped, because `\\y`
is a lie for Bangla). When a guard is chosen it applies to BOTH rules --
one field on the form, one meaning, and `preview` shows what it costs.

Transactions follow `app/services/admin.py`: these functions commit
their own writes, and roll back before raising a ValidationError for a
constraint the database caught.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from psycopg.errors import UniqueViolation

from app.db.connection import DictConnection
from app.db.watchlist import (
    MFS_CONTEXT,
    TELECOM_CONTEXT,
    TELECOM_CONTEXT_STRICT,
    WATCH_GROUPS,
    WatchEntity,
    WatchRule,
    all_entities,
    entity_where_sql,
)
from app.errors import ValidationError
from app.logging import get_logger
from app.services.matching import is_bengali

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════════
# Limits -- the input side of the injection defence
# ═════════════════════════════════════════════════════════════════════

# A term is one alternative inside the entity's pattern. Twelve per
# script is far more than any real entity needs (the widest hand-written
# entity in watchlist.py, `telecom_tax`, uses ten across both rules) and
# small enough that the resulting alternation stays a flat, linear scan.
MAX_TERMS_PER_SCRIPT = 12

# Sixty characters holds the longest real Bangla long-form name in the
# curated list (বাংলাদেশ টেলিযোগাযোগ নিয়ন্ত্রণ কমিশন is 43) with room to
# spare. The cap matters less for what it allows than for what it
# forbids: a pasted regex, a pasted paragraph, or a term long enough to
# make the pattern expensive.
MAX_TERM_LENGTH = 60

# One character -- in either script -- is a substring of almost every
# headline in the corpus. This is the same failure as রবি, one step
# worse, so it is refused rather than previewed.
MIN_TERM_LENGTH = 2

# There is deliberately no cap on rows scanned per preview, and no date
# window. The whole corpus is the point: a term has to be judged against
# everything we hold, and a preview restricted to "the last month" would
# let a term look clean purely because its false positives are older.
# Measured on the live corpus (78k headlines) a full preview -- two
# counts and two samples -- takes about one second.


# ═════════════════════════════════════════════════════════════════════
# Guards -- named, code-defined, never free text
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Guard:
    """One selectable context requirement. `pattern` is a cue set from
    `app/db/watchlist.py`, hand-tuned against the corpus; `label` and
    `blurb` are what the admin screen shows next to the radio button.
    """

    key: str
    label: str
    blurb: str
    pattern: str


# Why the guard is a CHOICE and not a second text box: a guard's whole
# job is to be a trustworthy set of domain cues, and the three below
# were each derived from measured false positives (see the constants'
# own comments in watchlist.py -- TELECOM_CONTEXT_STRICT exists purely
# because "tax" plus the loose cue set admitted a story about Chinese
# tariffs). A user-supplied guard would be one more untrusted pattern to
# escape, and a weak guard is worse than none: it makes the preview's
# guarded column look reassuring without actually removing anything.
GUARDS: dict[str, Guard] = {
    "telecom": Guard(
        key="telecom",
        label="Telecom context",
        blurb="The headline must also mention the sector: BTRC, an operator, spectrum, SIM, network, subscribers.",
        pattern=TELECOM_CONTEXT,
    ),
    "telecom_strict": Guard(
        key="telecom_strict",
        label="Telecom context (strict)",
        blurb=(
            "As above, minus the generic infrastructure words. For terms that are themselves common "
            "general-news words, like tax or duty."
        ),
        pattern=TELECOM_CONTEXT_STRICT,
    ),
    "mfs": Guard(
        key="mfs",
        label="Mobile financial services context",
        blurb="The headline must also mention wallets, MFS, mobile banking, merchants or payments.",
        pattern=MFS_CONTEXT,
    ),
}


def available_guards() -> list[Guard]:
    """The guard vocabulary, for the admin screen to render."""
    return list(GUARDS.values())


# ═════════════════════════════════════════════════════════════════════
# Turning what the operator typed into a pattern
# ═════════════════════════════════════════════════════════════════════

# Escaped by `_escape_term`: every ASCII character that is neither
# alphanumeric nor a space. That set is a superset of the POSIX ARE
# metacharacters, and Postgres documents `\` followed by a
# non-alphanumeric as that character taken literally, so backslashing
# them is always safe and never changes meaning.
#
# Non-ASCII characters are deliberately NOT escaped. Bangla codepoints
# have no special meaning in a POSIX regex, and `\` followed by a
# non-ASCII letter is not a defined escape -- backslashing Bangla would
# be, at best, undefined and at worst an error at query time.
#
# Verified against this database: 'a.*b' escaped does not match "axxxb"
# and does match the literal "a.*b"; '(a+)+' escaped does not match
# "aaaa" and does match the literal "(a+)+"; escaped Bangla still
# matches. tests/test_watchlist_admin.py pins all of it.
def _escape_term(term: str) -> str:
    out: list[str] = []
    for ch in term:
        if ch.isascii() and not ch.isalnum() and ch != " ":
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _clean_terms(terms: list[str], *, field: str, bangla: bool) -> list[str]:
    """Validate, normalise and de-duplicate one script's terms.

    Lowercased because `watchlist.NORMALIZED_TEXT_SQL` lowercases the
    headline and matches with the case-SENSITIVE `~` operator -- an
    uppercase term would simply never match, which is the worst possible
    failure here because it looks like "no coverage" rather than like an
    error.

    The script check is not pedantry. A Bangla term typed into the
    English box would be wrapped in `\\y` boundaries that do nothing for
    Bangla, producing exactly the রবি-matches-রবিবার bug while looking
    guarded. A Latin term in the Bangla box loses the boundaries it
    should have had. Either way the operator gets a pattern that does
    not mean what the form told them it means, so both are refused with
    a message that says which box to use.
    """
    cleaned: list[str] = []
    for raw in terms:
        term = " ".join(raw.strip().lower().split())
        if not term:
            continue
        if len(term) < MIN_TERM_LENGTH:
            raise ValidationError(
                f"{field} term {raw!r} is too short: a single character matches almost every headline"
            )
        if len(term) > MAX_TERM_LENGTH:
            raise ValidationError(f"{field} term {raw!r} is longer than {MAX_TERM_LENGTH} characters")
        # A term with no letter or digit in it survives escaping as a
        # string of backslashed punctuation, which is a valid pattern
        # that can never match a headline. The entity then saves happily
        # and reports zero forever -- indistinguishable, on the board,
        # from an entity nobody is writing about. MIN_TERM_LENGTH already
        # guards the opposite failure (a pattern that matches everything);
        # this guards the one that matches nothing.
        if not any(ch.isalnum() for ch in term):
            raise ValidationError(
                f"{field} term {raw!r} has no letters or digits in it, so it could never match a headline"
            )
        if bangla and not is_bengali(term):
            raise ValidationError(
                f"{field} term {raw!r} has no Bangla in it -- put Latin-script terms in the English field"
            )
        if not bangla and is_bengali(term):
            raise ValidationError(
                f"{field} term {raw!r} is Bangla -- put it in the Bangla field, where it will not be given "
                "word boundaries that do not work for Bangla"
            )
        if term not in cleaned:
            cleaned.append(term)
    if len(cleaned) > MAX_TERMS_PER_SCRIPT:
        raise ValidationError(f"at most {MAX_TERMS_PER_SCRIPT} {field} terms, got {len(cleaned)}")
    return cleaned


def _alternation(terms: list[str], *, boundaries: bool) -> str:
    """`(term|term|term)`, escaped, optionally `\\y`-bounded.

    The boundaries go on only where they can do anything: `\\y` asserts a
    transition between a word character and a non-word character, so
    putting one next to a term that starts or ends with punctuation
    ("4g+") produces a pattern that can never match. Trimming it there
    keeps the pattern honest instead of silently dead.
    """
    parts: list[str] = []
    for term in terms:
        escaped = _escape_term(term)
        if boundaries:
            prefix = "\\y" if term[0].isalnum() or term[0] == "_" else ""
            suffix = "\\y" if term[-1].isalnum() or term[-1] == "_" else ""
            escaped = f"{prefix}{escaped}{suffix}"
        parts.append(escaped)
    return "(" + "|".join(parts) + ")"


def _build_rules(en_terms: list[str], bn_terms: list[str], guard: Guard | None) -> tuple[WatchRule, ...]:
    """The rule set for a proposed entity: at most one Latin rule and one
    Bangla rule, in that order.

    The guard, when present, is applied to BOTH. The alternative --
    guarding only the Bangla rule, as the curated `bkash`/`nagad`
    entities do -- is right when a human picked which token was
    ambiguous, and wrong for a form with one guard checkbox: "gp" is
    Latin and needed the guard more than any Bangla token in the file.
    Applying it uniformly means the preview's two totals are a
    like-for-like comparison of the same term set, which is what makes
    the difference between them readable.
    """
    context = guard.pattern if guard else None
    en = _clean_terms(en_terms, field="English", bangla=False)
    bn = _clean_terms(bn_terms, field="Bangla", bangla=True)
    if not en and not bn:
        raise ValidationError("at least one English or Bangla term is required")

    rules: list[WatchRule] = []
    if en:
        rules.append(WatchRule(pattern=_alternation(en, boundaries=True), context=context))
    if bn:
        # No `\y`. See this module's docstring: it is TRUE for রবিবার
        # against রবি, so adding it would advertise a precision the
        # pattern does not have.
        rules.append(WatchRule(pattern=_alternation(bn, boundaries=False), context=context))
    return tuple(rules)


def _resolve_guard(context: str | None) -> Guard | None:
    if context is None or context == "":
        return None
    guard = GUARDS.get(context)
    if guard is None:
        raise ValidationError(f"unknown guard {context!r}, must be one of {sorted(GUARDS)}")
    return guard


# ═════════════════════════════════════════════════════════════════════
# Preview
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class PreviewSample:
    title: str
    source: str
    day: date
    score: float | None


@dataclass(frozen=True)
class PreviewResult:
    unguarded_total: int
    guarded_total: int | None
    unguarded_sample: list[PreviewSample]
    guarded_sample: list[PreviewSample]
    suggest_guard: bool


# When `preview` tells the operator the guard is worth taking.
#
# Measured on the live corpus (78k headlines), share of matches the
# guard removes:
#
#     রবি          + telecom     310 -> 4     98.7%   ambiguous
#     \ygp\y       + telecom      15 -> 3     80.0%   ambiguous
#     নগদ          + mfs          64 -> 19    70.3%   ambiguous
#     বিকাশ        + mfs          65 -> 27    58.5%   ambiguous
#     grameenphone + telecom      24 -> 24     0.0%   unambiguous
#     btrc         + telecom      28 -> 28     0.0%   unambiguous
#     \ybkash\y    + mfs          35 -> 35     0.0%   unambiguous
#
# Every known-ambiguous term removes more than half its matches; every
# term already precise enough to stand alone removes none, because a
# headline that names the operator satisfies the telecom cue by naming
# it. The gap between those two populations is enormous, so the exact
# threshold barely matters -- 0.5 sits in the middle of it and is a
# share a human can check by eye against the two sample lists.
#
# What the flag does NOT claim. `\ystarlink\y` + telecom is 16 -> 2
# (87.5%) and Starlink is a perfectly precise term; it loses matches
# only because the cue set does not list it. So a raised flag means
# "the guard changes this answer a lot, read both sample lists", not
# "your term is wrong". The UI must say it that way.
_MATERIAL_SHARE = 0.5

# Below this many unguarded matches, a percentage is not a measurement.
# Four matches dropping to one is 75% and tells the operator nothing;
# it is also the normal state of a brand-new entity in a corpus that
# has not covered it yet.
_MIN_MATCHES_FOR_A_SHARE = 20


def _count(conn: DictConnection, rules: tuple[WatchRule, ...]) -> int:
    where_sql, params = entity_where_sql(_probe(rules))
    row = conn.execute(
        f"SELECT count(*) AS n FROM articles a WHERE {where_sql}",  # noqa: S608 -- structure only; every pattern is a bound parameter
        params,
    ).fetchone()
    return int(row["n"]) if row else 0


def _sample(conn: DictConnection, rules: tuple[WatchRule, ...], limit: int) -> list[PreviewSample]:
    where_sql, params = entity_where_sql(_probe(rules))
    rows = conn.execute(
        f"""
        SELECT a.title, a.title_en, a.source, a.day, a.llm_score
        FROM articles a
        WHERE {where_sql}
        ORDER BY a.day DESC, a.id DESC
        LIMIT %s
        """,  # noqa: S608 -- structure only; every pattern is a bound parameter
        [*params, limit],
    ).fetchall()
    return [
        PreviewSample(
            # The Bangla headline is what the pattern actually matched
            # for most sources; the translation is the fallback for the
            # English-language outlets, which store an empty `title`
            # only when enrichment produced nothing either.
            title=row["title"] or row["title_en"] or "",
            source=row["source"],
            day=row["day"],
            score=float(row["llm_score"]) if row["llm_score"] is not None else None,
        )
        for row in rows
    ]


def _probe(rules: tuple[WatchRule, ...]) -> WatchEntity:
    """A throwaway entity so the preview runs through exactly the same
    `entity_where_sql` the dashboard and rollup will run later. If the
    preview built its own SQL, the operator could be shown numbers the
    saved entity never reproduces.
    """
    return WatchEntity(key="_preview", label="", note="", rules=rules)


def preview(
    conn: DictConnection,
    *,
    en_terms: list[str],
    bn_terms: list[str],
    context: str | None,
    sample: int = 5,
) -> PreviewResult:
    """Run a proposed entity against the whole corpus, twice.

    This is the feature. `unguarded_total` is what the terms match on
    their own; `guarded_total` is what survives the chosen context cue
    (None when no guard was proposed). Both samples come back so the
    screen can put them side by side -- the operator is being asked to
    read actual headlines, not to trust a number, because reading them
    is what caught রবিবার in the first place.

    Raises ValidationError for anything wrong with the terms, so a
    preview and the `create` that follows it fail identically rather
    than the save being the first place a limit is discovered.
    """
    guard = _resolve_guard(context)
    unguarded_rules = _build_rules(en_terms, bn_terms, guard=None)
    limit = max(0, min(sample, 50))

    unguarded_total = _count(conn, unguarded_rules)
    unguarded_sample = _sample(conn, unguarded_rules, limit) if limit else []

    guarded_total: int | None = None
    guarded_sample: list[PreviewSample] = []
    if guard is not None:
        guarded_rules = _build_rules(en_terms, bn_terms, guard=guard)
        guarded_total = _count(conn, guarded_rules)
        guarded_sample = _sample(conn, guarded_rules, limit) if limit else []

    suggest = False
    if guarded_total is not None and unguarded_total >= _MIN_MATCHES_FOR_A_SHARE:
        removed = unguarded_total - guarded_total
        suggest = removed / unguarded_total >= _MATERIAL_SHARE

    logger.info(
        "watch_entity_previewed",
        extra={
            "guard": guard.key if guard else "",
            "unguarded_total": unguarded_total,
            "guarded_total": guarded_total,
            "suggest_guard": suggest,
        },
    )
    return PreviewResult(
        unguarded_total=unguarded_total,
        guarded_total=guarded_total,
        unguarded_sample=unguarded_sample,
        guarded_sample=guarded_sample,
        suggest_guard=suggest,
    )


# ═════════════════════════════════════════════════════════════════════
# Create / list / delete
# ═════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CustomEntity:
    id: int
    group_key: str
    key: str
    label: str
    note: str
    rules: tuple[WatchRule, ...]
    created_by: str
    created_at: datetime


_GROUP_KEYS = frozenset(group.key for group in WATCH_GROUPS)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slug(label: str) -> str:
    """A stable `watch_entities.key` from the label.

    The key is not typed by the operator: it is an identifier that ends
    up in a rollup primary key and a dashboard payload, and asking a
    risk officer to invent one is asking for 'Robi ' with a trailing
    space. Derived keys are also what makes the collision check below
    meaningful -- a label of "Grameenphone" produces the code-defined
    key and is refused, rather than quietly becoming a second entity
    with the same name.

    A label with no Latin characters at all (an entity named only in
    Bangla) slugs to nothing, so it falls back to a short hash of the
    label. Deterministic, so the same label collides with itself on the
    UNIQUE constraint exactly as a Latin one would.
    """
    base = _SLUG_STRIP.sub("_", label.lower()).strip("_")
    if len(base) < 2 or not base[0].isalpha():
        digest = hashlib.sha256(label.encode("utf-8")).hexdigest()[:8]
        base = f"custom_{digest}"
    return base[:63]


def _row_to_entity(row: dict[str, Any], rules: tuple[WatchRule, ...]) -> CustomEntity:
    return CustomEntity(
        id=int(row["id"]),
        group_key=row["group_key"],
        key=row["key"],
        label=row["label"],
        note=row["note"],
        rules=rules,
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


def create(
    conn: DictConnection,
    *,
    group_key: str,
    label: str,
    note: str,
    en_terms: list[str],
    bn_terms: list[str],
    context: str | None,
    created_by: str,
) -> CustomEntity:
    """Save an entity the operator has just previewed.

    Refuses, in this order: an unknown group, an unknown guard, terms
    that break the limits, a label that slugs onto a code-defined
    entity's key, and a label that already exists. All of them raise
    ValidationError (app/errors.py), which the API boundary maps to 422.
    """
    if group_key not in _GROUP_KEYS:
        raise ValidationError(f"unknown watch group {group_key!r}, must be one of {sorted(_GROUP_KEYS)}")
    clean_label = " ".join(label.strip().split())
    if not clean_label:
        raise ValidationError("a label is required")
    if len(clean_label) > 120:
        raise ValidationError("label is longer than 120 characters")

    guard = _resolve_guard(context)
    rules = _build_rules(en_terms, bn_terms, guard=guard)

    key = _slug(clean_label)
    # The collision check that migrations/0026 explains cannot be a
    # database constraint: the code-defined keys live in a Python tuple
    # Postgres cannot see. Sharing a key would collide on
    # rollup_monthly_watch's (month, group_key, entity) primary key and
    # one entity's counts would overwrite the other's.
    if key in {entity.key for _group, entity in all_entities()}:
        raise ValidationError(
            f"{clean_label!r} collides with the built-in watchlist entity {key!r}; "
            "it is already tracked, with a hand-checked pattern"
        )

    try:
        row = conn.execute(
            """
            INSERT INTO watch_entities (group_key, key, label, note, created_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id, group_key, key, label, note, created_by, created_at
            """,
            (group_key, key, clean_label, note.strip(), created_by.strip().lower()),
        ).fetchone()
        assert row is not None
        for position, rule in enumerate(rules):
            conn.execute(
                "INSERT INTO watch_entity_rules (entity_id, position, pattern, context) VALUES (%s, %s, %s, %s)",
                (row["id"], position, rule.pattern, rule.context),
            )
    except UniqueViolation as exc:
        conn.rollback()
        raise ValidationError(f"a watchlist entity for {clean_label!r} already exists") from exc
    conn.commit()

    logger.info(
        "watch_entity_created",
        extra={"entity": key, "group_key": group_key, "created_by": created_by, "guard": guard.key if guard else ""},
    )
    return _row_to_entity(dict(row), rules)


def list_custom(conn: DictConnection) -> list[CustomEntity]:
    """Every operator-added entity, rules included, for the admin screen.

    Unlike `app.db.watchlist.custom_entities`, this returns rows the
    dashboard refuses to render -- one whose group no longer exists in
    code, say -- because the admin screen is where someone has to be
    able to see and delete them.
    """
    rows = conn.execute(
        """
        SELECT e.id, e.group_key, e.key, e.label, e.note, e.created_by, e.created_at,
               r.pattern, r.context
        FROM watch_entities e
        LEFT JOIN watch_entity_rules r ON r.entity_id = e.id
        ORDER BY e.group_key, e.key, r.position, r.id
        """
    ).fetchall()

    heads: dict[int, dict[str, Any]] = {}
    rules: dict[int, list[WatchRule]] = {}
    for row in rows:
        entity_id = int(row["id"])
        heads.setdefault(entity_id, dict(row))
        rules.setdefault(entity_id, [])
        if row["pattern"] is not None:
            rules[entity_id].append(WatchRule(pattern=row["pattern"], context=row["context"]))
    return [_row_to_entity(head, tuple(rules[entity_id])) for entity_id, head in heads.items()]


def delete(conn: DictConnection, entity_id: int) -> bool:
    """Remove one custom entity. Its rules go with it (0026's FK
    cascade). Returns False if there was no such entity, so the router
    can answer 404 rather than pretending.

    Historical rows already written to `rollup_monthly_watch` for this
    entity are deliberately left alone: they are a record of what the
    dashboard showed, and this module does not own that table.
    """
    row = conn.execute("DELETE FROM watch_entities WHERE id = %s RETURNING id", (entity_id,)).fetchone()
    conn.commit()
    if row is not None:
        logger.info("watch_entity_deleted", extra={"entity_id": entity_id})
    return row is not None

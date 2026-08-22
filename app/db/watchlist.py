"""The telecom risk watchlist: a small, curated, auditable set of
entities a Grameenphone CRO actually carries on a risk register, and the
exact text patterns that identify them in a headline.

WHY THIS EXISTS AT ALL (and why it does NOT reuse `keywords`)
─────────────────────────────────────────────────────────────
`articles.keywords` already records which configured keyword terms a
headline matched, so the obvious cheap move is "count keyword hits per
month and call it a competitor trend". Measured against the real corpus,
that move produces fiction:

  * The term "robi axiata" (term_bn "রবি আজিয়াটা, রবি") matched 222
    articles. Two of them are about the operator. The rest are Bangla
    substring collisions: রবিবার (Sunday), রবিউল (a given name), and so
    on, because the matcher looks for the bare token রবি inside the
    headline.
  * The term "gp" matched Formula 1 ("Belgian GP", "Hungarian GP") and
    UK health reporting ("GP surgery"), neither of which is the operator.
  * "spectrum" (term_bn "স্পেকট্রাম, তরঙ্গ") matched a run of celebrity
    stories, because অন্তরঙ্গ ("intimate") ends in তরঙ্গ.

A "Robi coverage is up 8x" chart built on that data would be a
fabricated measurement, which is worse than no chart. The keyword
config is tuned for RECALL into the review queue (an analyst reads the
false positives and discards them). A dashboard number has nobody
downstream to discard anything, so it needs precision instead.

So the watchlist below is deliberately separate, hand-checked against
the real corpus, and biased to precision over recall:

  * Latin-script tokens use `\\y` word boundaries, which work correctly
    for Latin text (`\\yrobi\\y` matches "Robi's" and not "Robiul").
  * Bangla tokens are spelled out in their unambiguous long form
    (রবি আজিয়াটা, not রবি), because `\\y` does NOT help for Bangla in
    this database's collation: `'রবিবার' ~ '\\yরবি\\y'` is TRUE.
  * A token that is genuinely ambiguous in isolation ("GP", "সেবার মান")
    carries a CONTEXT requirement: the same headline must also contain
    a telecom cue. See `WatchRule.context`.
  * The known Bangla substring trap তরঙ্গ ⊂ অন্তরঙ্গ is removed by
    `NORMALIZED_TEXT_SQL` before any pattern runs.

The cost of that bias is stated honestly on the screen: these are
counts of headlines we can positively identify, and an entity's real
coverage is at least this number, possibly a little more. That is a
defensible thing to put in front of a Chief Risk Officer. A number
inflated 100x by Sunday is not.

WHY THESE ENTITIES
──────────────────
Two groups, both chosen for what a CRO does with them rather than for
what is easy to count:

`operators` — the four licensed mobile operators plus the satellite
entrant. A peer's fine, outage, or licence condition is the single best
available forecast of your own, because the regulator applies the same
rule to every operator. Grameenphone itself is included so the board can
be read as share of voice, not just as a competitor list.

`regulatory` — the axes along which regulatory and policy pressure
actually arrives at a Bangladeshi MNO: the regulator itself (BTRC),
spectrum and licensing, telecom-specific tax and duty, SIM/subscriber
registration rules, service-quality mandates, network shutdown and
throttling, and international bandwidth infrastructure. These are the
line items an ERM register carries; "Geopolitical" and "Macroeconomy"
are not.

Adding an entity here is still a code change on purpose. Each pattern
in this file was eyeballed against its real matches in the corpus
before landing, and that review is the whole value of the file.

SELF-SERVICE ENTITIES, AND WHERE THE GUARD RAIL WENT
────────────────────────────────────────────────────
This file used to end by saying a self-service admin field "would
re-import the precision problem this module exists to solve". The owner
asked for self-service anyway, so the guard rail moved instead of being
removed: an operator may add an entity (migrations/0026, read back by
`custom_entities` below), but `app/services/watchlist_admin.py` first
runs the proposed pattern against the real corpus and shows its
matches, guarded and unguarded, with both totals. The eyeball pass that
used to happen in code review happens in the preview. Nothing reaches
`watch_entities` that the operator has not seen matching real
headlines.

Two consequences for this module:

  * `WATCH_GROUPS` and `all_entities()` are unchanged and still
    code-only -- `app/db/rollups.py::_upsert_watch` and
    `app/api/routers/dashboard.py` iterate them, and the entities they
    return are exactly the fifteen defined below. Custom entities are
    ADDITIVE, via `all_entities_with_custom(conn)`.
  * A code-defined pattern is a module constant and can be interpolated
    into SQL (`entity_predicate_sql`). A database-defined pattern is
    DATA and must never be. `entity_where_sql` therefore returns the
    SQL and its bind parameters separately, and is the only correct way
    to run a custom entity's rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.db.connection import DictConnection
from app.errors import ValidationError
from app.logging import get_logger

logger = get_logger(__name__)

# Headline text every pattern below runs against, as a SQL expression.
#
# * `title` is the source headline (Bangla for most sources), `title_en`
#   the English translation when the enrichment stage produced one. Both
#   are searched, joined by a separator that cannot appear inside either
#   and cannot bridge two words into an accidental match.
# * lower() so patterns can be written in lowercase and matched with the
#   case-SENSITIVE `~` operator, which keeps `\y` semantics predictable.
# * The অন্তরঙ্গ removal is the তরঙ্গ ("spectrum"/"wave") substring trap
#   documented in the module docstring: অন্তরঙ্গ means "intimate" and
#   ends with the exact characters তরঙ্গ, so celebrity stories otherwise
#   land in the spectrum-policy row.
#
# The template exists because `entity_where_sql` (added for custom
# entities) has to build the same expression for a caller's own table
# alias. One definition, so the two can never drift apart -- a preview
# that normalized text differently from the rollup would show the
# operator matches the dashboard then fails to reproduce.
_TEXT_SQL_TEMPLATE = (
    "lower(replace(coalesce({alias}.title, '') || ' ~ ' || coalesce({alias}.title_en, ''), 'অন্তরঙ্গ', ' '))"
)

NORMALIZED_TEXT_SQL = _TEXT_SQL_TEMPLATE.format(alias="a")

# A SQL identifier we are willing to splice into a query string.
_SAFE_ALIAS = re.compile(r"^[a-z_][a-z0-9_]*$")


def normalized_text_sql(table_alias: str = "a") -> str:
    """`NORMALIZED_TEXT_SQL` for an arbitrary table alias.

    `table_alias` is spliced into SQL, so it is checked against a
    conservative identifier pattern rather than trusted. Callers pass a
    literal from their own code ("a", "art"); anything else is a bug,
    and a bug that reaches this function is one that would otherwise
    reach the query planner.
    """
    if not _SAFE_ALIAS.match(table_alias):
        raise ValidationError(f"unsafe SQL table alias {table_alias!r}")
    return _TEXT_SQL_TEMPLATE.format(alias=table_alias)


# A headline mentions telecom at all. Used as the `context` for tokens
# that are real in this domain but ambiguous in isolation -- "GP" is the
# operator in "BTRC fines GP over illegal VoIP SIM use" and a doctor in
# "St Helens MP raises fears as thousands face GP surgery switch".
TELECOM_CONTEXT = (
    r"(btrc|বিটিআরসি|telecom|টেলিকম|টেলিযোগাযোগ|operator|অপারেটর|spectrum|স্পেকট্রাম|তরঙ্গ"
    r"|\ysims?\y|সিম কার্ড|network|নেটওয়ার্ক|subscriber|গ্রাহক|mobile|মোবাইল|voip|4g|5g"
    r"|grameenphone|গ্রামীণফোন|banglalink|বাংলালিংক|teletalk|টেলিটক|robi axiata|আজিয়াটা)"
)

# A stricter cue set, used only where the ambiguous token is itself a
# high-frequency general-news word. "Tax" plus the loose context above
# admitted "How India infiltrated China's network to evade US tariffs":
# `network` is a telecom cue in most headlines and not in that one.
# Dropping the generic infrastructure words and keeping the named
# operators, the regulator, spectrum, SIM and handsets leaves only
# headlines that are about telecom taxation specifically.
TELECOM_CONTEXT_STRICT = (
    r"(btrc|বিটিআরসি|telecom|টেলিকম|টেলিযোগাযোগ|mobile operator|মোবাইল অপারেটর|spectrum|স্পেকট্রাম|তরঙ্গ"
    r"|\ysims?\y|সিম কার্ড|handset|মোবাইল ফোন|মোবাইল সেট|\ygp\y"
    r"|grameenphone|গ্রামীণফোন|banglalink|বাংলালিংক|teletalk|টেলিটক|robi axiata|আজিয়াটা)"
)


# Mobile-financial-services cue. Required by the Bangla tokens for the
# two wallets below, because both spell ordinary words: নগদ is "cash"
# and বিকাশ is "development". Unguarded, নগদ collects cash robberies and
# Bangladesh Bank's cash-margin circulars, and বিকাশ collects economic
# development and the given name Bikash -- 78 and 84 raw matches against
# 33 and 46 real ones. This is the রবি/তরঙ্গ problem in a third place.
MFS_CONTEXT = (
    r"(\ymfs\y|mobile financial|মোবাইল ফাইন্যান্সিয়াল|এমএফএস|mobile banking|মোবাইল ব্যাংকিং"
    r"|\ywallet\y|ওয়ালেট|\ybkash\y|\ynagad\y|\yrocket\y|রকেট|\yupay\y|cashless|ক্যাশলেস"
    r"|\yqr\y|কিউআর|সার্ভিস চার্জ|service charge|মার্চেন্ট|merchant|পেমেন্ট|payment)"
)


@dataclass(frozen=True)
class WatchRule:
    """One alternative way an entity can be identified.

    `pattern` is a Postgres POSIX regex matched against
    NORMALIZED_TEXT_SQL. `context`, when set, is a SECOND pattern that
    must ALSO match the same headline -- the precision guard for
    ambiguous tokens. An entity matches if ANY of its rules match.
    """

    pattern: str
    context: str | None = None


@dataclass(frozen=True)
class WatchEntity:
    key: str
    label: str
    note: str
    rules: tuple[WatchRule, ...]


@dataclass(frozen=True)
class WatchGroup:
    key: str
    title: str
    blurb: str
    entities: tuple[WatchEntity, ...]


OPERATORS = WatchGroup(
    key="operators",
    title="Operator watch",
    blurb=(
        "The four licensed mobile operators and the satellite entrant. A peer's fine, outage or "
        "licence condition is the best available forecast of your own, because the regulator "
        "applies the same rule to everyone."
    ),
    entities=(
        WatchEntity(
            key="grameenphone",
            label="Grameenphone",
            note="Us. Included so the board reads as share of voice, not just a competitor list.",
            rules=(
                WatchRule(r"(grameenphone|গ্রামীণফোন)"),
                # The bare token, admitted only alongside a telecom cue.
                # Without the guard it collects Formula 1 and UK GP
                # surgeries; with it, it correctly picks up headlines
                # like "BTRC fines GP over illegal VoIP SIM use".
                WatchRule(r"\ygp\y", context=TELECOM_CONTEXT),
            ),
        ),
        WatchEntity(
            key="robi",
            label="Robi Axiata",
            note="Long-form Bangla only: the bare token রবি also spells Sunday and a common given name.",
            rules=(WatchRule(r"(\yrobi\y|রবি আজিয়াটা|axiata|আজিয়াটা)"),),
        ),
        WatchEntity(
            key="banglalink",
            label="Banglalink",
            note="Includes its parent VEON.",
            rules=(WatchRule(r"(banglalink|বাংলালিংক|\yveon\y|ভিওন)"),),
        ),
        WatchEntity(
            key="teletalk",
            label="Teletalk",
            note="State-owned operator. Its treatment sets the precedent for policy asks.",
            rules=(WatchRule(r"(teletalk|টেলিটক)"),),
        ),
        WatchEntity(
            key="starlink",
            label="Starlink",
            note="Satellite entrant. Substitution risk at the edge of the licensed market.",
            rules=(WatchRule(r"(starlink|স্টারলিংক)"),),
        ),
        WatchEntity(
            key="airtel",
            label="Airtel / Cirkle",
            note=(
                "Robi's second brand, renamed Cirkle in 2026. Tracked separately from "
                "Robi: a brand relaunch is its own competitive event."
            ),
            rules=(
                WatchRule(r"(\yairtel\y|এয়ারটেল|\ycirkle\y)"),
                # সার্কেল on its own is just "circle" -- clubs, cricket
                # circles, administrative circles. Admitted only next to
                # a telecom cue, which is what picks up the Bangla
                # rebrand coverage ("এয়ারটেলের নতুন পরিচয় সার্কেল").
                WatchRule(r"সার্কেল", context=TELECOM_CONTEXT),
            ),
        ),
    ),
)


REGULATORY = WatchGroup(
    key="regulatory",
    title="Regulatory pressure",
    blurb=(
        "The axes along which policy actually arrives at an operator. These are risk-register line "
        "items, which is what makes them worth trending. A section called Geopolitical is not."
    ),
    entities=(
        WatchEntity(
            key="btrc",
            label="BTRC actions",
            note="The regulator itself: fines, directives, hearings, licence decisions.",
            rules=(WatchRule(r"(\ybtrc\y|বিটিআরসি|বাংলাদেশ টেলিযোগাযোগ নিয়ন্ত্রণ কমিশন)"),),
        ),
        WatchEntity(
            key="spectrum",
            label="Spectrum and licensing",
            note="Auctions, renewals, repricing, licence grants and cancellations.",
            rules=(
                WatchRule(r"(spectrum|স্পেকট্রাম|তরঙ্গ)"),
                WatchRule(r"(licen[cs]e|লাইসেন্স|নিবন্ধন সনদ)", context=TELECOM_CONTEXT),
            ),
        ),
        WatchEntity(
            key="telecom_tax",
            label="Telecom tax and duty",
            note="Sector-specific tax, plus the revenue authority when it appears in a telecom story.",
            rules=(
                WatchRule(r"(sim tax|telecom tax|সিম কর|টেলিকম কর|supplementary duty|সম্পূরক শুল্ক)"),
                WatchRule(
                    r"(\ytax(es|ation|ed)?\y|\yduty\y|\yduties\y|শুল্ক|\yvat\y|ভ্যাট|\ylevy\y"
                    r"|revenue board|\ynbr\y|এনবিআর|জাতীয় রাজস্ব বোর্ড)",
                    context=TELECOM_CONTEXT_STRICT,
                ),
            ),
        ),
        WatchEntity(
            key="sim_rules",
            label="SIM and subscriber rules",
            note="Registration, NID linkage, per-NID caps, NEIR handset registration, biometrics.",
            rules=(
                WatchRule(r"(\ysims?\y|সিম কার্ড|সিম নিবন্ধন|সিম বিক্রি|\yneir\y|এনইআইআর)"),
                WatchRule(r"(biometric|বায়োমেট্রিক|\ynid\y|এনআইডি)", context=TELECOM_CONTEXT),
            ),
        ),
        WatchEntity(
            key="service_quality",
            label="Service quality mandates",
            note="Drive tests, call-drop rules, QoS directives. Compliance cost lands here first.",
            rules=(
                WatchRule(r"(call drop|কল ড্রপ|drive test|ড্রাইভ টেস্ট)"),
                WatchRule(
                    r"(service quality|সেবার মান|network quality|নেটওয়ার্কের মান|quality of service)",
                    context=TELECOM_CONTEXT,
                ),
            ),
        ),
        WatchEntity(
            key="shutdown",
            label="Shutdown and throttling",
            note="Network suspension and bandwidth curbs, here and in comparable markets.",
            rules=(
                WatchRule(
                    r"(internet shutdown|internet blackout|ইন্টারনেট শাটডাউন|ইন্টারনেট বন্ধ"
                    r"|mobile internet (shut|suspend|ban)|throttl)"
                ),
            ),
        ),
        WatchEntity(
            key="bandwidth",
            label="Bandwidth and cable",
            note="Submarine cable, IIG/ITC/NTTN, international bandwidth pricing and outages.",
            rules=(
                WatchRule(r"(submarine cable|সাবমেরিন কেবল|সাবমেরিন ক্যাবল|\yiig\y|\yittc\y|\ynttn\y)"),
                WatchRule(r"(bandwidth|ব্যান্ডউইথ)", context=TELECOM_CONTEXT),
            ),
        ),
    ),
)


MFS = WatchGroup(
    key="mfs",
    title="Wallets and payments",
    blurb=(
        "The two mobile-financial-services players an operator competes with for the same "
        "subscriber wallet. The scoring rubric already treats MFS as top-tier; this is the board "
        "that makes their moves countable."
    ),
    entities=(
        WatchEntity(
            key="bkash",
            label="bKash",
            note="Bangla token guarded: বিকাশ also means development, and Bikash is a given name.",
            rules=(
                WatchRule(r"\ybkash\y"),
                WatchRule(r"বিকাশ", context=MFS_CONTEXT),
            ),
        ),
        WatchEntity(
            key="nagad",
            label="Nagad",
            note="Bangla token guarded: নগদ is the ordinary word for cash.",
            rules=(
                WatchRule(r"\ynagad\y"),
                WatchRule(r"নগদ", context=MFS_CONTEXT),
            ),
        ),
    ),
)


WATCH_GROUPS: tuple[WatchGroup, ...] = (OPERATORS, REGULATORY, MFS)


def all_entities() -> list[tuple[WatchGroup, WatchEntity]]:
    return [(group, entity) for group in WATCH_GROUPS for entity in group.entities]


def prefilter_pattern() -> str:
    """One regex matching any headline that could possibly hit ANY
    watchlist entity: the union of every rule's `pattern`, ignoring
    `context` (a context only ever narrows a rule, so dropping it here
    keeps this a strict superset and cannot lose a row).

    Purely a performance guard. Evaluating twelve entity predicates
    against all 45,000 articles of a busy month costs seconds, because
    each is a regex over text Postgres cannot index. Almost none of
    those articles mention telecom at all, so one combined pass narrows
    the set to a few hundred rows and the per-entity work then happens
    on those. Same answer, one pass instead of twenty.
    """
    return "(" + "|".join(rule.pattern for _group, entity in all_entities() for rule in entity.rules) + ")"


def entity_predicate_sql(entity: WatchEntity, text_sql: str = NORMALIZED_TEXT_SQL) -> str:
    """The SQL boolean for "this row mentions `entity`".

    Every literal woven in here comes from this module's own frozen
    dataclasses -- never from a request, a database row, or an admin
    field -- so the patterns are code, not data, and there is no
    injection surface. `text_sql` is likewise a module constant or a
    test's own alias. Callers pass the result to a `count(*) FILTER
    (WHERE ...)` clause; see app/db/rollups.py::_upsert_watch.
    """
    clauses = []
    for rule in entity.rules:
        one = f"{text_sql} ~ '{rule.pattern}'"
        if rule.context:
            one = f"({one} AND {text_sql} ~ '{rule.context}')"
        clauses.append(one)
    return "(" + " OR ".join(clauses) + ")"


# ═══════════════════════════════════════════════════════════════════════
# Custom (database-defined) entities -- migrations/0026_watch_entities.sql
# ═══════════════════════════════════════════════════════════════════════


def entity_where_sql(entity: WatchEntity, table_alias: str = "a") -> tuple[str, list[str]]:
    """The SQL boolean for "this row mentions `entity`", with every
    pattern BOUND rather than interpolated.

    Returns `(sql, params)`: `sql` contains `%s` placeholders in the
    order `params` lists them, so a caller writes

        where_sql, params = entity_where_sql(entity)
        conn.execute(f"SELECT count(*) FROM articles a WHERE {where_sql}", params)

    This is the counterpart to `entity_predicate_sql`, and the two exist
    separately on purpose. `entity_predicate_sql` inlines its patterns,
    which is safe ONLY because the patterns it is ever given come from
    this module's own frozen dataclasses; `app/db/rollups.py` needs that
    form because it builds one aggregate with a dozen `count(*) FILTER
    (WHERE ...)` clauses, where a positional parameter per pattern would
    be unreadable and easy to mis-order. A custom entity's pattern comes
    out of a database row that an operator's form put there, so it is
    data: it goes in as a parameter, always, even though
    `app/services/watchlist_admin.py` has already escaped every regex
    metacharacter out of it. Escaping is the first line of defence, not
    the only one.

    Semantics match `entity_predicate_sql` exactly: a rule with a
    `context` requires BOTH patterns to match the same headline, and the
    entity matches if ANY rule matches. An entity with no rules yields
    `false` -- it cannot match anything, and saying so in SQL is safer
    than returning an empty string a caller might splice into a WHERE.
    """
    text = normalized_text_sql(table_alias)
    clauses: list[str] = []
    params: list[str] = []
    for rule in entity.rules:
        if rule.context:
            clauses.append(f"({text} ~ %s AND {text} ~ %s)")
            params.append(rule.pattern)
            params.append(rule.context)
        else:
            clauses.append(f"{text} ~ %s")
            params.append(rule.pattern)
    if not clauses:
        return "false", []
    return "(" + " OR ".join(clauses) + ")", params


_CODE_ENTITY_KEYS = frozenset(entity.key for group in WATCH_GROUPS for entity in group.entities)
_GROUPS_BY_KEY = {group.key: group for group in WATCH_GROUPS}


def custom_entities(conn: DictConnection) -> list[tuple[WatchGroup, WatchEntity]]:
    """The operator-added entities from `watch_entities`, shaped exactly
    like `all_entities()`'s rows so every consumer can treat the two the
    same way.

    Two classes of row are dropped rather than returned, both defending
    against a hand-written INSERT that skipped
    `app/services/watchlist_admin.py::create` (which refuses both):

      * a key that shadows a code-defined entity. Two entities sharing
        one key collide on `rollup_monthly_watch`'s (month, group_key,
        entity) primary key, and one silently overwrites the other's
        counts -- a wrong number on a risk dashboard, which is the exact
        failure mode this whole module exists to prevent.
      * a `group_key` no group in code has. The group supplies the title
        and blurb the dashboard renders around the entity; there is
        nothing to render it in.

    Both are logged at warning level, and the rows stay in the table so
    the admin screen can still list and delete them.
    """
    rows = conn.execute(
        """
        SELECT e.id, e.group_key, e.key, e.label, e.note, r.pattern, r.context
        FROM watch_entities e
        LEFT JOIN watch_entity_rules r ON r.entity_id = e.id
        ORDER BY e.group_key, e.key, r.position, r.id
        """
    ).fetchall()

    # {entity key: (group_key, label, note, [rules])}, insertion-ordered
    # by the query's ORDER BY.
    collected: dict[str, tuple[str, str, str, list[WatchRule]]] = {}
    for row in rows:
        key = row["key"]
        entry = collected.setdefault(key, (row["group_key"], row["label"], row["note"], []))
        if row["pattern"] is not None:
            entry[3].append(WatchRule(pattern=row["pattern"], context=row["context"]))

    out: list[tuple[WatchGroup, WatchEntity]] = []
    for key, (group_key, label, note, rules) in collected.items():
        if key in _CODE_ENTITY_KEYS:
            logger.warning("watch_entity_shadows_code_key", extra={"entity": key})
            continue
        group = _GROUPS_BY_KEY.get(group_key)
        if group is None:
            logger.warning("watch_entity_unknown_group", extra={"entity": key, "group_key": group_key})
            continue
        out.append((group, WatchEntity(key=key, label=label, note=note, rules=tuple(rules))))
    return out


def all_entities_with_custom(conn: DictConnection) -> list[tuple[WatchGroup, WatchEntity]]:
    """`all_entities()` plus the operator-added ones, ordered group by
    group (code-defined entities first within each group, then custom
    ones by key) so a dashboard can render the merged list in one pass
    and a custom entity never appears above the curated ones it sits
    beside.

    `all_entities()` is deliberately NOT redefined in terms of this
    function: callers that must stay code-only -- anything that
    interpolates patterns into SQL, and any test asserting the fixed
    vocabulary -- keep the version that cannot be changed from a form.
    """
    by_group: dict[str, list[WatchEntity]] = {}
    for group, entity in custom_entities(conn):
        by_group.setdefault(group.key, []).append(entity)

    out: list[tuple[WatchGroup, WatchEntity]] = []
    for group in WATCH_GROUPS:
        out.extend((group, entity) for entity in group.entities)
        out.extend((group, entity) for entity in sorted(by_group.get(group.key, []), key=lambda e: e.key))
    return out

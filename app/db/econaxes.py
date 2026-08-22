"""The economy axes: the six macro lines a Grameenphone risk desk carries
alongside the telecom watchlist, and the exact text patterns that
identify them in a headline.

WHY THIS EXISTS, AND WHY IT MIRRORS `app.db.watchlist`
─────────────────────────────────────────────────────
`app/db/watchlist.py` answers "what is the regulator doing to us, and
what are our competitors doing". It deliberately stops there: its
docstring notes that "Geopolitical" and "Macroeconomy" are not ERM
register line items. That is correct for a TELECOM watchlist and wrong
as a description of what an operator's risk register actually contains,
because a mobile operator in Bangladesh carries real, quantified
macro exposure that has nothing to do with BTRC:

  * tower and network operating cost is an ENERGY cost -- diesel,
    grid tariff, load-shedding hours on the backup fleet;
  * capex and spectrum fees are DOLLAR-denominated while revenue is in
    taka, so the FX line is a direct P&L exposure, not context;
  * handset and equipment duty arrives through the NBR and the budget;
  * the banking sector is where the receivables and the MFS float sit.

So these six axes are the macro half of the same board. They are
counted the same way, held to the same precision standard, and rendered
by the same evidence layer (`app/services/evidence.py`) as the telecom
entities, so a reader can put "BTRC actions" and "FX and reserves" side
by side and know both numbers mean the same thing.

THE SAME PRECISION DISCIPLINE, FOR THE SAME REASON
──────────────────────────────────────────────────
Everything `watchlist.py`'s docstring says about `articles.keywords`
applies here unchanged: a dashboard number has nobody downstream to
discard false positives, so these patterns are biased to PRECISION over
recall and every one of them was read against its real matches in the
corpus before landing. This module therefore reuses that module's
`NORMALIZED_TEXT_SQL` rather than defining its own notion of "the
headline text", and reuses its `pattern` + `context` rule shape.

Measured on the real corpus (July-August 2026, the window in which
collection was actually running), the traps below were found by reading
matches, not by guessing. Each is recorded next to the guard that fixes
it, because the guard is meaningless without the evidence for it.

THE BANGLA SUBSTRING TRAP, THREE MORE TIMES
───────────────────────────────────────────
`watchlist.py` documents তরঙ্গ ⊂ অন্তরঙ্গ and strips the superstring in
`NORMALIZED_TEXT_SQL` before any pattern runs. The economy vocabulary
hits the identical problem in three new places, and `ECON_STRIP_RE`
below is the same fix composed on top of that constant:

  * বিদ্যুৎ ("electricity") ⊂ বিদ্যুৎস্পৃষ্ট / বিদ্যুৎপৃষ্ট /
    বিদ্যুৎস্পর্শ / বিদ্যুতায়িত ("electrocuted"). 89 of the energy
    axis's 648 raw matches were electrocution deaths -- a man stealing
    a transformer, a construction worker, three people in Chapainawabganj.
    Not one of the 89 had ever been scored by the pipeline, which is
    the tell: they are crime-and-accident stories that never got near
    the risk queue. Removing them cost the axis ZERO scored articles
    and zero critical ones (286/172/mean 7.1 before and after).
  * ভ্যাট ("VAT") ⊂ ভ্যাটিকান ("Vatican"). "Professor Yunus signs
    Vatican declaration on AI and nuclear weapons" is not a tax story.
  * পোশাক ("clothing") ⊂ সাদাপোশাক ("plainclothes", as in plainclothes
    police). "সাদাপোশাকে তুলে নেওয়া সাংবাদিক" is an abduction story.

`\\y` word boundaries are NOT a substitute for any of this, for the
reason watchlist.py gives: `\\y` does not behave for Bangla in this
database's collation. It is worth recording that it does work for the
one case that matters most here -- `'ব্যাংকক' ~ '\\yব্যাংক\\y'` is FALSE,
so Bangkok does not leak into the banking axis -- but that is luck
about where a consonant cluster falls, not a rule to rely on.

WHAT WAS DELETED RATHER THAN GUARDED
────────────────────────────────────
Two tokens were tried, measured, and removed outright, because no
context could rescue them without smuggling the wrong stories back in:

  * a bare `\\ytariffs?\\y` in `trade_rmg` collected ELECTRICITY tariff
    stories, which belong to `energy`. The axis keeps `us tariff` and
    `শুল্ক আরোপ` instead, which are trade-policy specific.
  * a bare `stock market` in `capital_markets` collected the Indian
    market live-blogs ("Stock Market LIVE Updates, Sensex Today:
    Sensex Drops 650 Points As Oil Advances Amid US-Iran Tensions").
    Measured: it would have added 25 headlines, of which 5 score >= 8
    on the OIL story they are really about -- taking the axis's
    critical count from 2 to 7 and making it read as a Bangladeshi
    capital-markets emergency that did not happen.

Adding an axis is a code change, on purpose, for exactly the reason
watchlist.py gives: the review that produced these guards IS the value.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.watchlist import NORMALIZED_TEXT_SQL

# Superstrings removed before any economy pattern runs -- the same
# device as `NORMALIZED_TEXT_SQL`'s অন্তরঙ্গ removal, for the three
# Bangla substring traps documented in the module docstring, plus the
# English "reserve" senses that have nothing to do with FX.
#
# The English half is here because `\yreserves?\y` is the fx axis's
# highest-value token and also matches wildlife: 12 of its 51 raw
# matches were nature/tiger/forest reserves ("How Benfield Hill Nature
# Reserve helps glow worm population", "Injured Tigress Found In
# Pilibhit Tiger Reserve Dies"), and the commodity-stockpile senses
# (oil/fuel/food/strategic reserves) belong to the energy axis, not to
# a taka-versus-dollar line. All of them are fixed collocations, so
# removing the collocation leaves a genuine FX mention in the same
# headline still matching -- "Oil price volatility in the global
# market; pressure on reserves" is kept, because there "reserves" is
# not preceded by a commodity word.
ECON_STRIP_RE = (
    r"বিদ্যুৎ(স্পৃষ্ট|পৃষ্ট|স্পর্শ)|বিদ্যুতায়িত"
    r"|ভ্যাটিকান"
    r"|সাদা ?পোশাক"
    r"|(nature|wildlife|forest|tiger|game|marine|bird) reserves?"
    r"|reserves? forest|রিজার্ভ ফরেস্ট"
    r"|(oil|fuel|energy|food|gas|coal|grain|strategic) reserves?"
    r"|reserve fuel"
)


def normalized_text_sql(table_alias: str = "a") -> str:
    """Watchlist's `NORMALIZED_TEXT_SQL`, retargeted to `table_alias`.

    That constant is written against the alias `a` (it reads `a.title`
    and `a.title_en` and nothing else), so a caller that joined
    `articles` under a different alias gets those two references
    rewritten. The substitution is deliberately narrow -- those are the
    only two `a.` occurrences in it -- and `table_alias` is always a
    literal from this codebase, never caller input.
    """
    if table_alias == "a":
        return NORMALIZED_TEXT_SQL
    return NORMALIZED_TEXT_SQL.replace("a.", f"{table_alias}.")


def econ_text_sql(table_alias: str = "a") -> str:
    """The headline text every axis pattern runs against: the watchlist
    normalized text with `ECON_STRIP_RE` removed on top.

    Note for callers doing a two-stage scan: `ECON_STRIP_RE` only ever
    REMOVES characters, so a headline that fails a pattern against
    `normalized_text_sql` cannot start matching it once stripped. A
    cheap prefilter may therefore run against the unstripped text and
    still be a strict superset -- which is what `rollups._upsert_econ`
    relies on to keep the per-row `regexp_replace` off the full month.
    """
    return f"regexp_replace({normalized_text_sql(table_alias)}, '{ECON_STRIP_RE}', ' ', 'g')"


@dataclass(frozen=True)
class AxisRule:
    """One alternative way an axis can be identified.

    Identical in shape and meaning to `watchlist.WatchRule`: `pattern`
    is a Postgres POSIX regex matched against `econ_text_sql()`, and
    `context`, when set, is a SECOND pattern that must ALSO match the
    same headline. An axis matches if ANY of its rules match.
    """

    pattern: str
    context: str | None = None


@dataclass(frozen=True)
class EconAxis:
    key: str
    label: str
    note: str
    rules: tuple[AxisRule, ...]


# An apparel/trade cue. Required by পোশাক, which is simply the ordinary
# Bangla word for "clothing" and on its own collects Bollywood fashion
# shoots, wedding-dress runways, discount-brand roundups, an orangutan
# in a jumper, and children's bodies identified by their clothes -- 95
# raw matches against 70 real ones, of which the 25 rejected here carry
# 3 scored articles between them. Same failure mode as watchlist.py's
# নগদ ("cash") and বিকাশ ("development"): a real term that is also a
# high-frequency ordinary word.
RMG_CONTEXT = (
    r"(\yrmg\y|apparel|garment|তৈরি পোশাক|বিজিএমইএ|বিকেএমইএ|\ybgmea\y|\ybkmea\y"
    r"|রপ্তানি|রফতানি|export|কারখানা|factory|শ্রমিক|worker|মজুরি|\ywage|শিল্প|industry"
    r"|খাত\y|sector|বস্ত্র|textile|ক্রেতা|buyer|buying house|বায়িং হাউস|শুল্ক|tariff"
    r"|বাজার হারা|অর্ডার|man.made fiber|এমএমএফ|\ymmf\y|বৈশ্বিক বাজার|global market)"
)

# An FX-policy cue. Required by ডলার, which in this corpus is mostly
# just the UNIT any story quotes a large number in, not a currency
# signal: 207 of its 251 raw matches carry no FX cue at all, and they
# are Nolan's box office, FIFA's World Cup revenue, Elon Musk's net
# worth, Italy's cheese industry, a $90m museum theft, and Iran's
# bounty on US soldiers. Worse for this particular board, that set also
# includes the oil-price headlines ("Middle East tensions push crude to
# $92 per barrel") which score 9-10 -- so leaving ডলার unguarded moved
# 31 critical ENERGY articles onto the FX line and reported them as a
# reserves crisis.
FX_CONTEXT = (
    r"(\yforex\y|foreign exchange|বৈদেশিক মুদ্রা|\ytaka\y|টাকা|remittance|রেমিট্যান্স"
    r"|central bank|কেন্দ্রীয় ব্যাংক|বাংলাদেশ ব্যাংক|bangladesh bank|রিজার্ভ|\yreserves?\y"
    r"|exchange rate|বিনিময় হার|ডলারের (দাম|দর|রেট|বিনিময়)|ডলার সংকট|ডলার বাজার"
    r"|\yimf\y|আইএমএফ|আমদানি|রপ্তানি|রফতানি)"
)

# A Bangladesh-fiscal cue. Required by the two generic fiscal words,
# `budget`/`বাজেট` and `\yvat\y`, which otherwise report other
# countries' public finances as Bangladeshi risk: the US House's $95bn
# Iran war funding bill, the Pentagon's budget, "UK borrows more than
# expected in July as Healey prepares for first Budget", Indonesia's
# food-programme cut, Northern Ireland's electricity VAT, and -- the
# consumer-tech sense -- "I tested 3 great budget phones".
#
# This guard is the most expensive one in the file and is applied with
# that stated: it drops 48 of 96 `budget` matches, and roughly ten of
# those are real Bangladeshi budget-session stories whose English
# headline simply never names the country ("First budget session
# overshadowed by political confrontation"). That is the precision-over-
# recall bias watchlist.py's docstring commits to, made concrete: the
# axis undercounts rather than importing another country's fiscal news.
BD_FISCAL_CONTEXT = (
    r"(ভ্যাট|\ynbr\y|এনবিআর|জাতীয় রাজস্ব|revenue board|bangladesh|বাংলাদেশ|ঢাকা"
    r"|\ytaka\y|টাকা|অর্থবছর|অর্থমন্ত্র|finance minister|\yimf\y|আইএমএফ|রাজস্ব)"
)


ECON_AXES: tuple[EconAxis, ...] = (
    EconAxis(
        key="energy",
        label="Energy and power",
        note="LNG, gas, load-shedding, tariffs — tower operating cost.",
        rules=(
            AxisRule(
                r"লোডশেডিং|load.?shedding|\ylng\y|\yfsru\y|gas short|furnace oil"
                r"|বিদ্যুৎ|electricity tariff"
            ),
        ),
    ),
    EconAxis(
        key="trade_rmg",
        label="Trade and RMG",
        note="Apparel exports and US tariff exposure.",
        rules=(
            AxisRule(r"\yrmg\y|apparel|garment|তৈরি পোশাক"),
            # Trade-policy tariffs only. A bare `\ytariffs?\y` here
            # collected electricity-tariff stories, which are an energy
            # cost line and not an export-market exposure.
            AxisRule(r"us tariff|মার্কিন শুল্ক|শুল্ক আরোপ"),
            # The ordinary word for clothing -- see RMG_CONTEXT.
            AxisRule(r"পোশাক", context=RMG_CONTEXT),
        ),
    ),
    EconAxis(
        key="fx",
        label="FX and reserves",
        note="Taka, dollar rate, reserves — dollar-denominated capex and spectrum fees.",
        rules=(
            AxisRule(r"\yforex\y|foreign exchange|বৈদেশিক মুদ্রা"),
            # Wildlife and commodity-stockpile senses are already gone
            # from the text by ECON_STRIP_RE, so this token is left
            # unguarded on purpose: the real reserve headlines are
            # frequently bare ("Reserves rise further to $37.11 billion",
            # "Country's reserves rise"), and any context requirement
            # would drop the clearest signal the axis has.
            AxisRule(r"\yreserves?\y|রিজার্ভ"),
            AxisRule(r"dollar rate|ডলারের (দাম|দর|রেট)|taka deprec|টাকার মান|ডলার সংকট"),
            # The generic currency unit -- see FX_CONTEXT.
            AxisRule(r"ডলার", context=FX_CONTEXT),
        ),
    ),
    EconAxis(
        key="banking",
        label="Banking sector",
        note="Bangladesh Bank, NPLs, distressed loans, BSEC actions.",
        rules=(
            # `\yব্যাংক\y` is kept as-is after checking the one Bangla
            # boundary case that could have bitten: ব্যাংকক (Bangkok)
            # does NOT match, so the Speaker's Bangkok trip and the
            # Bangkok pet expo stay out, while ব্যাংকের / ব্যাংকিং /
            # ব্যাংকার all still match. Verified zero Bangkok leakage
            # across the whole window.
            #
            # The boundary is not free: ব্যাংককে is a true homograph --
            # the dative of ব্যাংক ("to the bank") AND the locative of
            # ব্যাংকক ("in Bangkok") -- so no pattern can admit one and
            # refuse the other, and `\y` refuses both. With ব্যাংকগুলোর
            # ("the banks'") that costs about 20 headlines per two-month
            # window against 802 kept. Measured and accepted.
            #
            # A bare `\ybanks?\y` was tried to close that gap and
            # rejected: it adds 486 headlines, mostly English bank
            # marketing (card launches, branch anniversaries, an Indian
            # apprentice result) and -- decisively -- the West Bank,
            # whose Israeli-Palestinian coverage scores up to 9.
            AxisRule(
                r"bangladesh bank|\ynpls?\y|distressed loan|\yব্যাংক\y"
                r"|central bank|\ybsec\y|merchant bank"
            ),
        ),
    ),
    EconAxis(
        key="revenue_tax",
        label="Revenue and tax",
        note="NBR, VAT, budget — sector-specific duty follows this line.",
        rules=(
            AxisRule(r"\ynbr\y|revenue board|রাজস্ব"),
            # Both generic fiscal words carry the Bangladesh guard --
            # see BD_FISCAL_CONTEXT. ভ্যাট is safe to name here only
            # because ECON_STRIP_RE has already removed ভ্যাটিকান.
            AxisRule(r"\yvat\y|ভ্যাট", context=BD_FISCAL_CONTEXT),
            AxisRule(r"\ybudget\y|বাজেট", context=BD_FISCAL_CONTEXT),
        ),
    ),
    EconAxis(
        key="capital_markets",
        label="Capital markets",
        note="DSEX, BSEC, listed-company notices.",
        rules=(
            # Named venues and regulators only. See the module docstring
            # on why a bare `stock market` was removed.
            AxisRule(r"\ydsex\y|\ybsec\y|শেয়ারবাজার|পুঁজিবাজার|ডিএসই"),
        ),
    ),
)


def axis_where_sql(axis: EconAxis, table_alias: str = "a") -> tuple[str, list[str]]:
    """The SQL boolean for "this row is on `axis`", plus its parameters.

    Unlike `watchlist.entity_predicate_sql`, which weaves its patterns
    straight into the SQL text, this returns `%s` placeholders and the
    patterns as bind parameters. Both are safe -- every pattern here is
    a frozen module constant, never a request value or an admin-editable
    field -- but parameters are the cheaper habit to keep, and they let
    `app/services/evidence.py` accept a predicate from EITHER this
    module or the watchlist without caring which produced it.

    Parameters come back in the order their placeholders appear, so a
    caller composing several predicates into one statement must
    concatenate the param lists in the same order it concatenates the
    SQL. See `app/db/rollups.py::_upsert_econ`.
    """
    text = econ_text_sql(table_alias)
    clauses: list[str] = []
    params: list[str] = []
    for rule in axis.rules:
        one = f"{text} ~ %s"
        params.append(rule.pattern)
        if rule.context:
            one = f"({one} AND {text} ~ %s)"
            params.append(rule.context)
        clauses.append(one)
    return "(" + " OR ".join(clauses) + ")", params


def econ_prefilter_pattern() -> str:
    """One regex matching any headline that could possibly hit ANY axis:
    the union of every rule's `pattern`, ignoring `context` (a context
    only ever narrows a rule, so dropping it here keeps this a strict
    superset and cannot lose a row).

    Purely a performance guard, and the exact counterpart of
    `watchlist.prefilter_pattern`. Evaluating every axis predicate
    against all 45,000 articles of a busy month costs seconds, because
    each is a regex over text Postgres cannot index. One combined pass
    narrows the set first and the per-axis work then happens on those.
    """
    return "(" + "|".join(rule.pattern for axis in ECON_AXES for rule in axis.rules) + ")"


def all_axes() -> tuple[EconAxis, ...]:
    return ECON_AXES


def axis_by_key(key: str) -> EconAxis | None:
    for axis in ECON_AXES:
        if axis.key == key:
            return axis
    return None

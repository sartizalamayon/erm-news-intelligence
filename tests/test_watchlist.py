"""The telecom watchlist (app/db/watchlist.py) and the two rollups it
feeds (migrations/0021).

WHY THIS FILE IS MOSTLY NEGATIVE ASSERTIONS. The watchlist exists
because the obvious data source for "how much Robi coverage was there
this month" -- `articles.keywords`, populated by the keyword matcher --
is wrong by two orders of magnitude, and wrong in ways that look
plausible on a chart. The measured failures in the real corpus were:

    "robi axiata"  matched 222 articles, 2 about the operator; the rest
                   were the Bangla substring রবি inside রবিবার (Sunday)
                   and রবিউল (a given name).
    "gp"           matched Formula 1 and UK GP surgeries.
    "spectrum"     matched celebrity stories, because অন্তরঙ্গ
                   ("intimate") ends in তরঙ্গ ("wave"/"spectrum").

Those three are pinned below as explicit non-matches. If someone later
"simplifies" a pattern back to a bare token, the dashboard would go back
to reporting Sunday as competitor coverage, and only a test that names
the exact failing headline would catch it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.queries import insert_article
from app.db.rollups import compute_rollups_for_month
from app.db.testing import migrated_schema, pg_available
from app.db.watchlist import (
    WATCH_GROUPS,
    WatchEntity,
    WatchRule,
    all_entities,
    all_entities_with_custom,
    custom_entities,
    entity_predicate_sql,
    entity_where_sql,
    prefilter_pattern,
)

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

MONTH = dt.date(2026, 7, 1)
_seq = iter(range(1, 10_000))


def _entity(key: str):
    for _group, entity in all_entities():
        if entity.key == key:
            return entity
    raise AssertionError(f"no watchlist entity {key!r}")


def _matches(conn, key: str, title: str, title_en: str = "") -> bool:
    """Does the real SQL predicate for `key` match this headline?

    Runs the exact expression `app.db.rollups` puts in its aggregate, so
    these assertions cannot drift from what actually populates the
    dashboard.
    """
    pred = entity_predicate_sql(_entity(key), text_sql="x.s")
    row = conn.execute(
        f"SELECT {pred} AS hit FROM ("  # noqa: S608 -- module constants only, see watchlist.py
        "  SELECT lower(replace(coalesce(%s, '') || ' ~ ' || coalesce(%s, ''), 'অন্তরঙ্গ', ' ')) AS s"
        ") x",
        (title, title_en),
    ).fetchone()
    return bool(row["hit"])


# ── The three measured false positives, pinned ──────────────────────────


@pytest.mark.parametrize(
    ("entity_key", "title", "title_en"),
    [
        # রবি is a substring of রবিবার (Sunday) and রবিউল (a name). This
        # single collision produced 220 phantom "Robi" articles.
        ("robi", "আগামী রবিবার ঢাকায় সমাবেশ", ""),
        ("robi", "রবিউল আউয়ালের চাঁদ দেখা যায়নি", ""),
        # The bare token GP without a telecom cue.
        ("grameenphone", "", "Hamilton, Antonelli stung by grid penalties at Hungarian GP"),
        ("grameenphone", "", "St Helens MP raises fears as thousands face GP surgery switch"),
        # অন্তরঙ্গ ("intimate") ends in তরঙ্গ ("spectrum"/"wave").
        ("spectrum", "কেটি পেরি-জাস্টিন ট্রুডোর অন্তরঙ্গ মুহূর্ত ভাইরাল", ""),
        # "Network" is a telecom cue in most headlines and not in this
        # one, which is why telecom tax uses the stricter cue set.
        ("telecom_tax", "", "How India infiltrated China's network to evade US tariffs"),
    ],
)
def test_known_false_positives_do_not_match(entity_key: str, title: str, title_en: str) -> None:
    with migrated_schema() as conn:
        assert not _matches(conn, entity_key, title, title_en)


@pytest.mark.parametrize(
    ("entity_key", "title", "title_en"),
    [
        ("grameenphone", "", "Grameenphone, Dekko Foods partner to drive digital transformation"),
        ("grameenphone", "গ্রামীণফোনকে ১৮ লাখ ৪০ হাজার টাকা জরিমানা", ""),
        # The bare token, rescued by a telecom cue in the same headline.
        ("grameenphone", "", "BTRC fines GP over illegal VoIP SIM use"),
        ("robi", "", "Robi Axiata's Q2 profit increases"),
        ("robi", "রবি আজিয়াটার পর্ষদ সভার তারিখ ঘোষণা", ""),
        ("banglalink", "", "High Spectrum Costs Hinder Network Investment: Banglalink"),
        ("teletalk", "৩৬ জুলাই উপলক্ষে ৩৬ টাকায় সিম দিচ্ছে টেলিটক", ""),
        ("starlink", "", "Banglalink to introduce Starlink's satellite mobile service in Bangladesh"),
        ("btrc", "", "BTRC launches study to reset mobile spectrum pricing"),
        ("btrc", "বিটিআরসি চেয়ারম্যানের সঙ্গে মেটার প্রতিনিধি দলের বৈঠক", ""),
        ("spectrum", "", "BTRC turns to eGSM spectrum despite border signal risks"),
        ("spectrum", "স্পেকট্রামের দাম কমানো এবং সেবার মান বাড়ানোর তাগিদ", ""),
        ("sim_rules", "", "BTRC to block additional SIMs; maximum 10 SIMs allowed per NID"),
        ("service_quality", "", "BTRC tightens drive tests to improve network checks"),
        ("shutdown", "", "26 July 2024: Block raids, key coordinators' detention, and internet shutdown"),
        # The full real headline, kept whole on purpose: "bandwidth"
        # alone is not admitted (it needs a telecom cue), and here BTRC
        # in the same headline is that cue.
        (
            "bandwidth",
            "BTRC Fined Tk 3 Crore Summit Communications | No reprieve for Summit over Tk 3cr bandwidth pricing fine",
            "",
        ),
        ("telecom_tax", "", "GP seeks integrated spectrum, revenue and tax policy"),
    ],
)
def test_real_headlines_from_the_corpus_do_match(entity_key: str, title: str, title_en: str) -> None:
    """Every headline here is a real one from the collected corpus."""
    with migrated_schema() as conn:
        assert _matches(conn, entity_key, title, title_en)


def test_prefilter_is_a_superset_of_every_entity_predicate() -> None:
    """The prefilter is a performance guard only. If it ever excluded a
    headline one of the entity predicates would have matched, the rollup
    would silently undercount, which is the exact class of bug this
    screen must not have.
    """
    with migrated_schema() as conn:
        cases = [
            ("", "BTRC fines GP over illegal VoIP SIM use"),
            ("গ্রামীণফোনকে ১৮ লাখ ৪০ হাজার টাকা জরিমানা", ""),
            ("স্পেকট্রামের দাম কমানো", ""),
            ("", "Robi Axiata's Q2 profit increases"),
            ("", "GP seeks integrated spectrum, revenue and tax policy"),
        ]
        for title, title_en in cases:
            row = conn.execute(
                "SELECT lower(replace(coalesce(%s,'') || ' ~ ' || coalesce(%s,''), 'অন্তরঙ্গ',' ')) ~ %s AS hit",
                (title, title_en, prefilter_pattern()),
            ).fetchone()
            hit_any = any(_matches(conn, e.key, title, title_en) for _g, e in all_entities())
            assert not hit_any or row["hit"], f"prefilter would drop a real match: {title or title_en}"


# ── The rollups the watchlist feeds ─────────────────────────────────────


def _article(url: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "url": url,
        "url_norm": url,
        "source": "Test Source",
        "title": "",
        "title_en": "",
        "day": dt.date(2026, 7, 15),
        "review_status": "pending",
        "sections": "",
        "topic_tag": "",
    }
    fields.update(overrides)
    return fields


def test_watch_rollup_counts_volume_and_severity_separately() -> None:
    with migrated_schema() as conn:
        insert_article(conn, **_article("https://x.test/1", title_en="BTRC fines four operators", llm_score=9.0))
        insert_article(conn, **_article("https://x.test/2", title_en="BTRC hearing on telecom services", llm_score=6.0))
        # Identified, but never scored: must lift article_count without
        # touching scored_count, so the mean severity stays honest.
        insert_article(conn, **_article("https://x.test/3", title_en="BTRC seeks local infrastructure"))
        # And a headline mentioning nothing on the watchlist at all.
        insert_article(conn, **_article("https://x.test/4", title_en="Cricket team announces squad", llm_score=9.0))
        conn.commit()

        compute_rollups_for_month(conn, MONTH)
        conn.commit()

        row = conn.execute(
            "SELECT article_count, scored_count, high_count, score_sum FROM rollup_monthly_watch "
            "WHERE month = %s AND entity = 'btrc'",
            (MONTH,),
        ).fetchone()
        assert row["article_count"] == 3
        assert row["scored_count"] == 2
        assert row["high_count"] == 1  # only the 9.0
        assert float(row["score_sum"]) == 15.0  # 9.0 + 6.0, mean 7.5 over 2

        # Every entity gets a row even at zero, so the 12-month series
        # has no holes the frontend has to guess about.
        written = conn.execute(
            "SELECT count(*) AS n FROM rollup_monthly_watch WHERE month = %s", (MONTH,)
        ).fetchone()
        assert written["n"] == len(all_entities())


def test_coverage_rollup_separates_monitored_from_backdated() -> None:
    """The distinction the whole long-term tab rests on: an article
    PUBLISHED in a month nobody was collecting in must not make that
    month look monitored.
    """
    with migrated_schema() as conn:
        old_month = dt.date(2026, 2, 1)
        # Published in February, fetched much later (the default now()).
        insert_article(conn, **_article("https://x.test/old", day=dt.date(2026, 2, 10), sections="Political"))
        conn.commit()

        compute_rollups_for_month(conn, old_month)
        conn.commit()

        row = conn.execute(
            "SELECT article_count, collected_count, collected_days FROM rollup_monthly_coverage WHERE month = %s",
            (old_month,),
        ).fetchone()
        assert row["article_count"] == 1  # the article is real and counted
        assert row["collected_count"] == 0  # but it was not collected that month
        assert row["collected_days"] == 0  # so February was never monitored


def test_watch_groups_are_the_three_a_risk_officer_asks_about() -> None:
    keys = [g.key for g in WATCH_GROUPS]
    # `mfs` joined operators/regulatory when bKash and Nagad were added:
    # the scoring rubric already treats mobile financial services as
    # top-tier, and an operator competes with both for the same wallet.
    assert keys == ["operators", "regulatory", "mfs"]
    operator_keys = {e.key for e in WATCH_GROUPS[0].entities}
    # Grameenphone itself is on the operator board on purpose: the board
    # is share of voice among peers, not a list of other people.
    assert "grameenphone" in operator_keys
    assert {"robi", "banglalink", "teletalk", "airtel"} <= operator_keys
    assert {e.key for e in WATCH_GROUPS[2].entities} == {"bkash", "nagad"}


# ── Custom entities (migrations/0026) ───────────────────────────────────
#
# Self-service entity creation lives in app/services/watchlist_admin.py
# and is tested there. What is tested HERE is the half that has to keep
# working exactly as it did: the code-defined vocabulary the rollup and
# the dashboard iterate must be unaffected by anything a form writes,
# and the parameterized SQL builder must agree with the interpolated one
# it sits beside.


def test_all_entities_is_the_fifteen_code_defined_ones_with_an_empty_custom_table() -> None:
    """`app/db/rollups.py::_upsert_watch` and the dashboard router both
    iterate `all_entities()`. If a custom table could change that count,
    the number of rows written per month would depend on what someone
    typed, and `entity_predicate_sql`'s "every literal comes from this
    module's frozen dataclasses" guarantee would be false.
    """
    with migrated_schema() as conn:
        assert len(all_entities()) == 15
        assert conn.execute("SELECT count(*) AS n FROM watch_entities").fetchone()["n"] == 0
        assert custom_entities(conn) == []
        assert all_entities_with_custom(conn) == all_entities()


def _insert_custom(conn, *, key: str, group_key: str, rules: list[tuple[str, str | None]]) -> int:
    row = conn.execute(
        "INSERT INTO watch_entities (group_key, key, label, note, created_by) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (group_key, key, key.title(), "", "ops@example.test"),
    ).fetchone()
    for position, (pattern, context) in enumerate(rules):
        conn.execute(
            "INSERT INTO watch_entity_rules (entity_id, position, pattern, context) VALUES (%s, %s, %s, %s)",
            (row["id"], position, pattern, context),
        )
    conn.commit()
    return int(row["id"])


def test_custom_entities_are_appended_inside_their_own_group() -> None:
    with migrated_schema() as conn:
        _insert_custom(
            conn,
            key="walletco",
            group_key="mfs",
            rules=[(r"(\ywalletco\y)", None), ("(ওয়ালেটকো)", "(ওয়ালেট)")],
        )

        pairs = all_entities_with_custom(conn)
        assert len(pairs) == 16
        group, entity = pairs[-1]
        # Last, and inside `mfs` -- a custom entity never sorts above
        # the curated ones it sits beside.
        assert group.key == "mfs"
        assert entity.key == "walletco"
        assert entity.rules == (
            WatchRule(pattern=r"(\ywalletco\y)"),
            WatchRule(pattern="(ওয়ালেটকো)", context="(ওয়ালেট)"),
        )


def test_a_custom_entity_shadowing_a_code_key_is_dropped() -> None:
    """A hand-written INSERT can do what the service refuses. Two
    entities with one key collide on rollup_monthly_watch's primary key
    and one overwrites the other's counts, so the merge drops the
    intruder rather than letting a wrong number reach the dashboard.
    """
    with migrated_schema() as conn:
        _insert_custom(conn, key="btrc", group_key="regulatory", rules=[("(anything)", None)])
        assert custom_entities(conn) == []
        assert all_entities_with_custom(conn) == all_entities()


def test_a_custom_entity_in_an_unknown_group_is_dropped() -> None:
    """The group supplies the title and blurb the dashboard renders
    around an entity; there is nothing to render an orphan in. The row
    stays in the table so the admin screen can still delete it.
    """
    with migrated_schema() as conn:
        _insert_custom(conn, key="walletco", group_key="competitors", rules=[("(walletco)", None)])
        assert custom_entities(conn) == []
        assert conn.execute("SELECT count(*) AS n FROM watch_entities").fetchone()["n"] == 1


def _where_matches(conn, entity: WatchEntity, title: str, title_en: str = "") -> bool:
    where_sql, params = entity_where_sql(entity)
    row = conn.execute(
        f"SELECT {where_sql} AS hit FROM (SELECT %s::text AS title, %s::text AS title_en) a",  # noqa: S608
        [*params, title, title_en],
    ).fetchone()
    return bool(row["hit"])


@pytest.mark.parametrize(
    ("title", "title_en"),
    [
        ("", "BTRC fines GP over illegal VoIP SIM use"),
        ("", "Hamilton, Antonelli stung by grid penalties at Hungarian GP"),
        ("আগামী রবিবার ঢাকায় সমাবেশ", ""),
        ("কেটি পেরি-জাস্টিন ট্রুডোর অন্তরঙ্গ মুহূর্ত ভাইরাল", ""),
        ("রাজধানীতে নগদ টাকা ছিনতাই", ""),
        ("নগদের মোবাইল ব্যাংকিং সেবায় নতুন মার্চেন্ট", ""),
        ("", "GP seeks integrated spectrum, revenue and tax policy"),
    ],
)
def test_entity_where_sql_gives_the_same_answer_as_entity_predicate_sql(title: str, title_en: str) -> None:
    """Two builders for one meaning: `entity_predicate_sql` interpolates
    (safe only for this module's own constants, and what the rollup's
    twelve-FILTER aggregate needs), `entity_where_sql` binds (required
    for a pattern that came out of a database row). They must never
    disagree, or a previewed entity would count differently from a
    curated one on the same screen.
    """
    with migrated_schema() as conn:
        for _group, entity in all_entities():
            assert _where_matches(conn, entity, title, title_en) == _matches(conn, entity.key, title, title_en), (
                f"{entity.key} disagrees on {title or title_en!r}"
            )


def test_entity_where_sql_binds_every_pattern_rather_than_inlining_it() -> None:
    """The property the whole custom-entity feature rests on: a pattern
    that came from a form is never spliced into SQL text, escaping or
    no escaping.
    """
    entity = WatchEntity(
        key="probe",
        label="",
        note="",
        rules=(WatchRule(pattern="(walletco)"), WatchRule(pattern="(ওয়ালেটকো)", context="(ওয়ালেট)")),
    )
    where_sql, params = entity_where_sql(entity)
    assert params == ["(walletco)", "(ওয়ালেটকো)", "(ওয়ালেট)"]
    assert where_sql.count("%s") == len(params)
    for pattern in params:
        assert pattern not in where_sql


def test_entity_where_sql_requires_both_patterns_when_a_rule_is_guarded() -> None:
    with migrated_schema() as conn:
        guarded = WatchEntity(
            key="probe",
            label="",
            note="",
            rules=(WatchRule(pattern="(নগদ)", context="(মোবাইল ব্যাংকিং)"),),
        )
        assert not _where_matches(conn, guarded, "রাজধানীতে নগদ টাকা ছিনতাই")
        assert _where_matches(conn, guarded, "নগদের মোবাইল ব্যাংকিং সেবা")

        # And ANY rule matching is enough.
        either = WatchEntity(
            key="probe",
            label="",
            note="",
            rules=(WatchRule(pattern="(নগদ)", context="(মোবাইল ব্যাংকিং)"), WatchRule(pattern=r"(\ynagad\y)")),
        )
        assert _where_matches(conn, either, "", "Nagad opens 500 new agent points")


def test_an_entity_with_no_rules_matches_nothing() -> None:
    with migrated_schema() as conn:
        empty = WatchEntity(key="probe", label="", note="", rules=())
        assert entity_where_sql(empty) == ("false", [])
        assert not _where_matches(conn, empty, "নগদ টাকা", "Nagad")

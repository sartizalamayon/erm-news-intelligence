"""Self-service watchlist entities (app/services/watchlist_admin.py,
migrations/0026).

WHY THIS FILE IS ADVERSARIAL. `app/db/watchlist.py` argues that adding a
watch entity should be a code change, because a form that turns typed
text into a regex re-imports the two failures that module exists to
prevent:

    precision  a Bangla term is always a substring search -- Postgres
               has no working word boundary for it, `'রবিবার' ~
               '\\yরবি\\y'` is TRUE -- so রবি matched 222 headlines of
               which two were the operator.
    injection  the typed text becomes a POSIX regex. `a.*b` would match
               most of the corpus; `(a+)+` is a backtracking bomb.

Self-service was asked for anyway, so the tests below pin the two things
that make it safe: user text is escaped into a literal before it is ever
a pattern, and a proposed entity is measured against the real corpus
(guarded and unguarded) before it can be saved. If either regressed, the
dashboard would go back to reporting Sunday as competitor coverage --
and it would look plausible, which is why the assertions are explicit
about the exact headline that must not match.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.db.watchlist import all_entities, all_entities_with_custom, entity_where_sql
from app.errors import ValidationError
from app.services import watchlist_admin as wa

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

_seq = iter(range(1, 100_000))


def _add(conn, title: str = "", title_en: str = "", **overrides: object) -> None:
    n = next(_seq)
    fields: dict[str, object] = {
        "url": f"https://x.test/{n}",
        "url_norm": f"https://x.test/{n}",
        "source": "Test Source",
        "title": title,
        "title_en": title_en,
        "day": dt.date(2026, 7, 15),
        "review_status": "pending",
    }
    fields.update(overrides)
    insert_article(conn, **fields)


# ── Injection: what the operator types is a literal, never a pattern ───


def test_a_wildcard_term_is_neutralised_into_a_literal() -> None:
    """`a.*b` submitted as a term must match the literal string "a.*b"
    and nothing else. Unescaped it would match "axxxb", and in a real
    corpus that is most of it.
    """
    with migrated_schema() as conn:
        _add(conn, title_en="axxxb is not a company")
        _add(conn, title_en="a.*b is the literal string")
        conn.commit()

        result = wa.preview(conn, en_terms=["a.*b"], bn_terms=[], context=None)
        assert result.unguarded_total == 1
        assert result.unguarded_sample[0].title == "a.*b is the literal string"


def test_a_backtracking_bomb_is_neutralised_and_does_not_hang() -> None:
    """`(a+)+` is the classic catastrophic-backtracking pattern. Escaped,
    it is five literal characters: it matches the literal text and
    nothing else, and the scan is linear.
    """
    with migrated_schema() as conn:
        for _ in range(30):
            _add(conn, title_en="a" * 40 + "!")
        _add(conn, title_en="the literal (a+)+ here")
        conn.commit()

        started = time.monotonic()
        result = wa.preview(conn, en_terms=["(a+)+"], bn_terms=[], context=None)
        elapsed = time.monotonic() - started

        assert result.unguarded_total == 1
        assert result.unguarded_sample[0].title == "the literal (a+)+ here"
        assert elapsed < 5.0, f"preview took {elapsed:.1f}s -- the pattern is backtracking"


def test_the_stored_pattern_contains_no_live_metacharacters() -> None:
    """The escaping has to survive the round trip to the database, since
    that stored pattern is what the rollup will run every night.
    """
    with migrated_schema() as conn:
        entity = wa.create(
            conn,
            group_key="operators",
            label="Metachar Telecom",
            note="",
            en_terms=["a.*b", "(a+)+", "c|d"],
            bn_terms=[],
            context=None,
            created_by="ops@example.test",
        )
        pattern = entity.rules[0].pattern
        # The only unescaped metacharacters left are the ones this
        # module wrote itself: the alternation between terms, its
        # wrapping parentheses, and the `\y` boundaries -- which are
        # dropped around a term that starts or ends with punctuation,
        # since a boundary there could never match anything.
        assert pattern == r"(\ya\.\*b\y|\(a\+\)\+|\yc\|d\y)"
        assert ".*" not in pattern.replace(r"\.", "").replace(r"\*", "")

        stored = wa.list_custom(conn)[0]
        assert stored.rules[0].pattern == pattern


def test_a_quoted_term_cannot_break_out_of_the_pattern() -> None:
    """A single quote in a term would end the literal if a pattern were
    ever interpolated into SQL. It is bound as a parameter instead, so
    it is simply part of the term.
    """
    with migrated_schema() as conn:
        _add(conn, title_en="o'brien telecom launches")
        _add(conn, title_en="unrelated headline")
        conn.commit()

        result = wa.preview(conn, en_terms=["o'brien"], bn_terms=[], context=None)
        assert result.unguarded_total == 1


# ── Precision: the Bangla/Latin asymmetry the module exists for ────────


def test_bangla_in_the_english_field_is_refused() -> None:
    """The English field wraps terms in `\\y`, which does nothing for
    Bangla. Accepting রবি there would produce a pattern that looks
    guarded and matches রবিবার -- the original 222-article bug.
    """
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="Bangla field"):
            wa.preview(conn, en_terms=["রবি আজিয়াটা"], bn_terms=[], context=None)


def test_latin_in_the_bangla_field_is_refused() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="English field"):
            wa.preview(conn, en_terms=[], bn_terms=["robi"], context=None)


def test_a_bangla_term_is_not_given_word_boundaries() -> None:
    """Pinned because it looks like an oversight. `\\y` around a Bangla
    term matches inside রবিবার anyway, so adding it would advertise a
    precision the pattern does not have -- the guard is the real
    mechanism, and the preview is how the operator sees that.
    """
    with migrated_schema() as conn:
        entity = wa.create(
            conn,
            group_key="mfs",
            label="Wallet Watch",
            note="",
            en_terms=["walletco"],
            bn_terms=["ওয়ালেটকো"],
            context="mfs",
            created_by="ops@example.test",
        )
        latin, bangla = entity.rules
        assert latin.pattern == r"(\ywalletco\y)"
        assert bangla.pattern == "(ওয়ালেটকো)"
        assert r"\y" not in bangla.pattern
        # The guard applies to both rules: one checkbox, one meaning.
        assert latin.context and bangla.context


def test_single_character_and_oversized_terms_are_refused() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="too short"):
            wa.preview(conn, en_terms=["x"], bn_terms=[], context=None)
        with pytest.raises(ValidationError, match="longer than"):
            wa.preview(conn, en_terms=["x" * 61], bn_terms=[], context=None)


def test_too_many_terms_are_refused() -> None:
    with migrated_schema() as conn:
        terms = [f"term{n}" for n in range(wa.MAX_TERMS_PER_SCRIPT + 1)]
        with pytest.raises(ValidationError, match="at most"):
            wa.preview(conn, en_terms=terms, bn_terms=[], context=None)


# ── The preview itself: guarded vs unguarded ───────────────────────────


def _cash_corpus(conn) -> None:
    """A miniature of the measured নগদ problem: the ordinary word for
    cash, mostly in stories that have nothing to do with the company.
    On the live corpus this term matches 64 headlines and 19 survive the
    MFS guard.
    """
    # Inserted oldest-first: the four real ones on earlier days, so the
    # sample's "most recent first" ordering puts the false positives at
    # the top of the unguarded list -- which is what the operator is
    # meant to notice.
    for n in range(4):
        _add(conn, title=f"নগদ-এর মোবাইল ব্যাংকিং সেবায় নতুন মার্চেন্ট যুক্ত {n}", day=dt.date(2026, 7, 10))
    for n in range(20):
        _add(conn, title=f"রাজধানীতে নগদ টাকা ছিনতাই, আটক {n}", day=dt.date(2026, 7, 15))
    conn.commit()


def test_the_guard_removes_the_false_positives_and_is_suggested() -> None:
    with migrated_schema() as conn:
        _cash_corpus(conn)

        result = wa.preview(conn, en_terms=[], bn_terms=["নগদ"], context="mfs", sample=3)

        assert result.unguarded_total == 24
        assert result.guarded_total == 4
        assert result.suggest_guard is True
        # Both lists come back so the screen can put them side by side:
        # the operator is being asked to read headlines, not to trust a
        # ratio.
        assert len(result.unguarded_sample) == 3
        assert len(result.guarded_sample) == 3
        assert all("মোবাইল ব্যাংকিং" in s.title for s in result.guarded_sample)
        assert all("ছিনতাই" in s.title for s in result.unguarded_sample)


def test_a_precise_term_keeps_all_its_matches_and_is_not_flagged() -> None:
    """The other half of the threshold's justification. A term precise
    enough to stand alone loses nothing to the guard, because a headline
    that names the company satisfies the cue set by naming it -- measured
    at 0% removed for grameenphone, btrc and bkash on the live corpus.
    """
    with migrated_schema() as conn:
        for n in range(25):
            _add(conn, title_en=f"Walletco expands its mobile banking merchant network {n}")
        conn.commit()

        result = wa.preview(conn, en_terms=["walletco"], bn_terms=[], context="mfs")
        assert result.unguarded_total == 25
        assert result.guarded_total == 25
        assert result.suggest_guard is False


def test_no_guard_proposed_means_no_guarded_total() -> None:
    with migrated_schema() as conn:
        _cash_corpus(conn)
        result = wa.preview(conn, en_terms=[], bn_terms=["নগদ"], context=None)
        assert result.unguarded_total == 24
        assert result.guarded_total is None
        assert result.guarded_sample == []
        assert result.suggest_guard is False


def test_a_share_is_not_computed_off_a_handful_of_matches() -> None:
    """4 matches dropping to 1 is 75% and means nothing -- it is also the
    normal state of an entity the corpus has barely covered yet.
    """
    with migrated_schema() as conn:
        for n in range(4):
            _add(conn, title=f"নগদ টাকা ছিনতাই {n}")
        conn.commit()

        result = wa.preview(conn, en_terms=[], bn_terms=["নগদ"], context="mfs")
        assert result.unguarded_total == 4
        assert result.guarded_total == 0
        assert result.suggest_guard is False


def test_preview_reports_what_the_operator_needs_to_judge_a_match() -> None:
    with migrated_schema() as conn:
        _add(conn, title_en="Walletco raises fees", source="Daily Sun", day=dt.date(2026, 7, 20), llm_score=7.5)
        conn.commit()

        sample = wa.preview(conn, en_terms=["walletco"], bn_terms=[], context=None).unguarded_sample[0]
        assert sample.title == "Walletco raises fees"
        assert sample.source == "Daily Sun"
        assert sample.day == dt.date(2026, 7, 20)
        assert sample.score == pytest.approx(7.5)


def test_an_unknown_guard_is_refused() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="unknown guard"):
            wa.preview(conn, en_terms=["walletco"], bn_terms=[], context="lenient")
        assert {g.key for g in wa.available_guards()} == {"telecom", "telecom_strict", "mfs"}


# ── create / list / delete ─────────────────────────────────────────────


def test_a_key_colliding_with_a_code_defined_entity_is_refused() -> None:
    """Two entities sharing a key collide on rollup_monthly_watch's
    (month, group_key, entity) primary key, and one silently overwrites
    the other's counts. Postgres cannot catch this -- the code-defined
    keys are a Python tuple -- so the service must.
    """
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="built-in"):
            wa.create(
                conn,
                group_key="operators",
                label="Grameenphone",
                note="",
                en_terms=["grameenphone"],
                bn_terms=[],
                context=None,
                created_by="ops@example.test",
            )
        assert wa.list_custom(conn) == []


def test_an_unknown_group_is_refused() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="unknown watch group"):
            wa.create(
                conn,
                group_key="competitors",
                label="Walletco",
                note="",
                en_terms=["walletco"],
                bn_terms=[],
                context=None,
                created_by="ops@example.test",
            )


def test_empty_terms_are_refused() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="at least one"):
            wa.create(
                conn,
                group_key="operators",
                label="Nothing",
                note="",
                en_terms=[],
                bn_terms=["   "],
                context=None,
                created_by="ops@example.test",
            )


def test_a_duplicate_label_is_refused() -> None:
    with migrated_schema() as conn:
        for _ in range(1):
            wa.create(
                conn,
                group_key="operators",
                label="Walletco",
                note="",
                en_terms=["walletco"],
                bn_terms=[],
                context=None,
                created_by="ops@example.test",
            )
        with pytest.raises(ValidationError, match="already exists"):
            wa.create(
                conn,
                group_key="operators",
                label="Walletco",
                note="",
                en_terms=["walletco"],
                bn_terms=[],
                context=None,
                created_by="ops@example.test",
            )
        assert len(wa.list_custom(conn)) == 1


def test_a_bangla_only_label_still_gets_a_usable_key() -> None:
    """The key ends up in a rollup primary key and a JSON payload, so it
    has to be ASCII-shaped. A label with no Latin in it falls back to a
    hash of the label, deterministically.
    """
    with migrated_schema() as conn:
        entity = wa.create(
            conn,
            group_key="mfs",
            label="ওয়ালেটকো",
            note="",
            en_terms=[],
            bn_terms=["ওয়ালেটকো"],
            context="mfs",
            created_by="ops@example.test",
        )
        assert entity.key.startswith("custom_")
        assert entity.key == wa._slug("ওয়ালেটকো")


def test_create_records_who_added_it_and_survives_a_round_trip() -> None:
    with migrated_schema() as conn:
        created = wa.create(
            conn,
            group_key="operators",
            label="Walletco Mobile",
            note="Rumoured entrant; pattern deliberately excludes the unrelated Walletco Foods.",
            en_terms=["walletco mobile", "walletco telecom"],
            bn_terms=["ওয়ালেটকো"],
            context="telecom",
            created_by="Ops@Example.Test",
        )
        assert created.key == "walletco_mobile"
        assert created.group_key == "operators"
        assert created.created_by == "ops@example.test"  # users.email, lowercased
        assert len(created.rules) == 2

        listed = wa.list_custom(conn)
        assert [e.key for e in listed] == ["walletco_mobile"]
        assert listed[0].rules == created.rules
        assert listed[0].created_at is not None
        assert listed[0].note.startswith("Rumoured entrant")


def test_a_saved_entity_matches_through_the_same_sql_the_preview_used() -> None:
    """The preview's promise is that the saved entity behaves the way
    the preview showed. Both go through entity_where_sql, and this pins
    that they agree on a real headline.
    """
    with migrated_schema() as conn:
        # নিওক্যাশ and not ওয়ালেটকো: ওয়ালেট ("wallet") is itself one of
        # the MFS cues, so a brand whose name contains it satisfies its
        # own guard -- the substring problem again, one level up.
        _add(conn, title="নিওক্যাশ-এর মোবাইল ব্যাংকিং সেবা চালু")
        _add(conn, title="নিওক্যাশ ফুডসের নতুন কারখানা")  # same token, no MFS cue
        conn.commit()

        preview = wa.preview(conn, en_terms=[], bn_terms=["নিওক্যাশ"], context="mfs")
        assert (preview.unguarded_total, preview.guarded_total) == (2, 1)

        wa.create(
            conn,
            group_key="mfs",
            label="Neocash",
            note="",
            en_terms=[],
            bn_terms=["নিওক্যাশ"],
            context="mfs",
            created_by="ops@example.test",
        )
        merged = {entity.key: entity for _group, entity in all_entities_with_custom(conn)}
        where_sql, params = entity_where_sql(merged["neocash"])
        row = conn.execute(
            f"SELECT count(*) AS n FROM articles a WHERE {where_sql}",  # noqa: S608 -- patterns are bound parameters
            params,
        ).fetchone()
        assert row["n"] == preview.guarded_total


def test_delete_removes_the_entity_and_its_rules() -> None:
    with migrated_schema() as conn:
        entity = wa.create(
            conn,
            group_key="operators",
            label="Walletco",
            note="",
            en_terms=["walletco"],
            bn_terms=[],
            context=None,
            created_by="ops@example.test",
        )
        assert wa.delete(conn, entity.id) is True
        assert wa.list_custom(conn) == []
        # The FK cascade, not an application-level second DELETE.
        assert conn.execute("SELECT count(*) AS n FROM watch_entity_rules").fetchone()["n"] == 0
        # Deleting it again is not an error, it is a 404 for the router.
        assert wa.delete(conn, entity.id) is False


def test_custom_entities_never_change_what_all_entities_returns() -> None:
    """`app/db/rollups.py` and the dashboard router iterate the
    code-defined list. A form must not be able to change what those see
    without an explicit call to the `_with_custom` variant.
    """
    with migrated_schema() as conn:
        before = [entity.key for _group, entity in all_entities()]
        wa.create(
            conn,
            group_key="operators",
            label="Walletco",
            note="",
            en_terms=["walletco"],
            bn_terms=[],
            context=None,
            created_by="ops@example.test",
        )
        assert [entity.key for _group, entity in all_entities()] == before
        assert "walletco" in [entity.key for _group, entity in all_entities_with_custom(conn)]

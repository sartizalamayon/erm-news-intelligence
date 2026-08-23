"""Retention and purge behaviour for the rebuilt Long-term dashboard —
the state where a COUNT has outlived the ARTICLE it was computed from.

WHY THIS FILE EXISTS SEPARATELY FROM tests/test_dashboard_long.py
─────────────────────────────────────────────────────────────────
That file proves the boards read the right numbers out of a populated
database. This one proves they still read the right numbers out of a
database the purge has been through, which is the screen's central
design claim and the one nothing exercised: `grep critExpired tests/`
returned nothing before this file.

The claim, stated plainly: `items`/`scored`/`critical`/`mean` come from
the monthly rollups (`rollup_monthly_watch`, `rollup_monthly_econ`),
which are bounded per-month aggregates and are not purged; the ≤3
headlines behind each row come from live `articles`, which are. So a
row can legitimately report "2 critical items" and be unable to show a
single headline, and `BoardRowOut.critExpired`
(app/api/schemas/dashboard_long.py) is what separates that from a row
where nothing happened. Both render differently
(frontend/js/screens/dashboard_long.js, `expiredNote`), and collapsing
them would turn a real risk event into a quiet month.

DRIVING THE REAL PURGE, NOT HAND-DELETING ROWS
──────────────────────────────────────────────
Every expiry here is produced by `app.services.admin.run_purge(
operation="matched")` → `app.services.storage.purge_matched_articles`,
through the genuine preview-then-confirm pair that ARCH-DB's RT-6 fix
requires — so these tests prove the interaction with the purge that
actually ships, not with a `DELETE FROM articles` that resembles it.

`purge_matched_articles` is the right purge to aim here and the only one
that can be: the other two cannot reach a scored, matched article at
all. `purge_stale_bodies` only empties `article_bodies`, and
`purge_stale_unmatched_articles` — the one that actually reads
`raw_retention_days` (`_resolve_cutoff_days`, default 90 in
app/db/settings.py) — is filtered to `review_status = 'filtered_out'`,
rows that never counted towards a board number in the first place.
`purge_matched_articles` takes an explicit `older_than` date instead of
a retention-days window, which is also what makes the partial-expiry
case below reachable: articles are seeded into two months and the cutoff
is placed between them, so the purge itself decides which headlines
survive.

`require_exported=False` throughout. The export-first gate is
tests/test_storage.py's subject, not this file's, and switching it off
keeps the seeding to the two facts these tests are about — a month, and
a score.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.db import rollups
from app.db.testing import migrated_schema, pg_available
from app.main import create_app
from app.services import admin as admin_service
from app.services import macro as macro_service

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(
    id=1, email="cro@grameenphone.com", role="admin", display_name="CRO", session_id=1
)

_url_seq = itertools.count()

# Two adjacent months, both inside the twelve-month window every endpoint
# computes from today. The purge cutoff always sits on the boundary
# between them, so nothing here depends on which day of the month the
# suite runs: LAST_MONTH is always strictly before the cutoff and
# THIS_MONTH is always on or after it.
THIS_MONTH = date.today().replace(day=1)
LAST_MONTH = (THIS_MONTH - timedelta(days=1)).replace(day=1)

# Headlines chosen so each one matches exactly ONE watchlist entity or
# economy axis, checked against `watchlist.entity_where_sql` and
# `econaxes.axis_where_sql` for every entity and axis in the code. That
# matters more here than in the ordinary board tests: these assertions
# compare a rollup row against a live-article row, and a headline
# leaking into a second entity would move a count without moving the
# headline behind it.
BTRC = "BTRC hearing on operator conduct"  # regulatory/btrc
SPECTRUM = "Spectrum auction timetable published"  # regulatory/spectrum
BANKING = "Bangladesh Bank orders distressed loan review"  # econ/banking


def _insert_article(conn, *, month: date, title: str, score: float | None = None, reason: str = "r") -> int:
    """One article published on the 1st of `month` and FETCHED inside it.

    `fetched_at` is set explicitly rather than left to its `now()`
    default because `rollups._upsert_coverage` derives `collected_days`
    from it, and `collected_days > 0` is what makes a month "monitored"
    for `dashboard_long._window`. A backdated article fetched today
    would leave its own month unmonitored, and the board would then
    correctly report nothing at all — for a reason that has nothing to
    do with retention.
    """
    row = conn.execute(
        "INSERT INTO articles (url, url_norm, source, title, day, fetched_at, llm_score, llm_reason) "
        "VALUES (%s, '', 'Prothom Alo', %s, %s, %s, %s, %s) RETURNING id",
        (
            f"https://x.test/a-{next(_url_seq)}",
            title,
            month,
            datetime(month.year, month.month, 1, 6, 0, tzinfo=timezone.utc),
            score,
            reason,
        ),
    ).fetchone()
    return int(row["id"])


def _compute_rollups(conn, *months: date) -> None:
    """The real rollup pass — the same entrypoint the daily batch calls."""
    for month in months:
        rollups.compute_rollups_for_month(conn, month)
    conn.commit()


def _mark_monitored(conn, month: date) -> None:
    """Run ONLY the coverage pass, so `month` counts as monitored while
    the watch rollup for it stays empty.

    `dashboard_long._window` treats a month as monitored once
    `_upsert_coverage` has recorded `collected_days > 0` for it, and
    `_board_row` short-circuits to zeros when no month is monitored —
    before it ever consults a rollup or falls back to live `articles`.
    So a test that wants to see the LIVE FALLBACK has to get the month
    monitored first. It cannot do that with the full pass, because that
    would also write the very watch row whose absence is the point.
    """
    start = rollups._month_start(month)
    rollups._upsert_coverage(conn, start, rollups._next_month(start))
    conn.commit()


def _purge_before(conn, cutoff: date) -> int:
    """Delete every article published before `cutoff`, through the real
    purge — preview first, then confirm, which is the only sequence
    `purge_matched_articles` accepts (app/services/storage.py's "Closing
    RT-6": a confirm with no matching snapshot raises rather than
    recomputing the predicate).
    """
    kwargs = {"operation": "matched", "older_than": cutoff, "require_exported": False}
    preview = admin_service.run_purge(conn, confirm=False, **kwargs)
    result = admin_service.run_purge(conn, confirm=True, **kwargs)
    conn.commit()
    assert result.confirmed is True
    assert result.rows_affected == preview.rows_affected, "the purge deleted a different set than it showed"
    return result.rows_affected


@pytest.fixture
def offline_macro(monkeypatch: pytest.MonkeyPatch):
    """No third party is reachable from a test, and none should be.

    Identical to tests/test_dashboard_long.py's fixture of the same name
    — tests/conftest.py blocks sockets process-wide, and the economy and
    risk tabs both call these fetchers on every request.
    """
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(macro_service, "fetch_usd_bdt", lambda: macro_service.MacroValue(122.0, now))
    monkeypatch.setattr(macro_service, "fetch_brent", lambda key: macro_service.MacroValue(93.6, now))
    monkeypatch.setattr(macro_service, "fetch_inflation", lambda: macro_service.MacroValue(8.77, now))
    monkeypatch.setattr(
        macro_service, "fetch_annual_exchange_rate", lambda: macro_service.AnnualValue(121.92, 2025, now)
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, offline_macro):
    import app.api.routers.dashboard_long as mod

    with migrated_schema() as conn:
        monkeypatch.setattr(mod, "get_connection", lambda: contextlib.nullcontext(conn))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        app.dependency_overrides[require_admin] = lambda: ADMIN
        with TestClient(app) as c:
            c.conn = conn  # type: ignore[attr-defined]
            yield c


def _watch_rows(client, board: str) -> dict[str, dict]:
    payload = client.get("/dashboard/long/telecom")
    assert payload.status_code == 200, payload.text
    return {r["key"]: r for r in payload.json()[board]["rows"]}


def _econ_rows(client) -> dict[str, dict]:
    payload = client.get("/dashboard/long/economy")
    assert payload.status_code == 200, payload.text
    return {r["key"]: r for r in payload.json()["board"]["rows"]}


def _article_count(conn) -> int:
    return int(conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"])


# ── the core case ────────────────────────────────────────────────────


def test_a_purged_month_keeps_its_counts_and_admits_the_headlines_are_gone(client):
    """The screen's central claim, end to end.

    Four BTRC articles in a monitored month, three of them scored and
    two of those critical. The rollup runs, then the purge takes every
    article. The row must still report 4 / 3 / 2 / 7.0 from the rollup,
    must show no headlines because there are none left to show, and must
    set `critExpired` so the screen can say why the list is empty.
    """
    conn = client.conn
    for score in (9.0, 8.0, 4.0):
        _insert_article(conn, month=LAST_MONTH, title=BTRC, score=score)
    _insert_article(conn, month=LAST_MONTH, title=BTRC, score=None)
    conn.commit()
    _compute_rollups(conn, LAST_MONTH)

    # Before the purge: the counts and the headlines agree.
    before = _watch_rows(client, "regulatory")["btrc"]
    assert (before["items"], before["scored"], before["critical"], before["mean"]) == (4, 3, 2, 7.0)
    assert len(before["crit"]) == 2
    assert before["critExpired"] is False

    assert _purge_before(conn, THIS_MONTH) == 4
    assert _article_count(conn) == 0
    surviving = conn.execute(
        "SELECT article_count, scored_count, high_count, score_sum FROM rollup_monthly_watch "
        "WHERE entity = 'btrc' AND month = %s",
        (LAST_MONTH,),
    ).fetchone()
    assert surviving is not None, "the rollup row is what is supposed to outlive the articles"

    after = _watch_rows(client, "regulatory")["btrc"]
    assert (after["items"], after["scored"], after["critical"], after["mean"]) == (4, 3, 2, 7.0)
    assert after["crit"] == [], "the headlines are gone, so there is nothing honest to list"
    assert after["critExpired"] is True
    # "0 of 2 shown" — the row is explicit that it is not listing them,
    # rather than implying two headlines fitted in an empty list.
    assert after["criticalTruncated"] is True


# ── the negative case ────────────────────────────────────────────────


def test_a_quiet_row_and_an_expired_row_do_not_render_the_same(client):
    """`critical == 0` with no articles is NOT `critExpired`.

    Both rows come back from the same purged window with an empty
    `crit`. If `critExpired` were simply "no headlines", the spectrum
    row — two articles, neither of them severe — would carry the "past
    the retention window" note and a reader would be told something had
    happened that never did.
    """
    conn = client.conn
    for score in (9.0, 8.0):
        _insert_article(conn, month=LAST_MONTH, title=BTRC, score=score)
    for score in (3.0, 4.0):
        _insert_article(conn, month=LAST_MONTH, title=SPECTRUM, score=score)
    conn.commit()
    _compute_rollups(conn, LAST_MONTH)
    _purge_before(conn, THIS_MONTH)

    rows = _watch_rows(client, "regulatory")
    quiet, expired = rows["spectrum"], rows["btrc"]

    assert (quiet["items"], quiet["scored"], quiet["critical"], quiet["mean"]) == (2, 2, 0, 3.5)
    assert quiet["crit"] == []
    assert quiet["critExpired"] is False, "nothing happened here — do not claim something did"
    assert quiet["criticalTruncated"] is False

    assert expired["critical"] == 2
    assert expired["critExpired"] is True
    assert quiet["critExpired"] != expired["critExpired"]


# ── partial expiry ───────────────────────────────────────────────────


def test_partial_expiry_shows_what_survived_and_counts_what_did_not(client):
    """Half the evidence purged, half retained.

    Four critical BTRC articles last month, one this month, cutoff
    between them. The count is the full five from the rollup; the list
    is the one article still on disk; `criticalTruncated` says so; and
    `critExpired` is False because the row can still be opened. Marking
    it expired here would tell a reader the list is empty when it is
    not.
    """
    conn = client.conn
    for _ in range(4):
        _insert_article(conn, month=LAST_MONTH, title=BTRC, score=9.0, reason="last month")
    survivor = _insert_article(conn, month=THIS_MONTH, title=BTRC, score=8.5, reason="this month")
    conn.commit()
    _compute_rollups(conn, LAST_MONTH, THIS_MONTH)

    assert _purge_before(conn, THIS_MONTH) == 4
    assert _article_count(conn) == 1

    row = _watch_rows(client, "regulatory")["btrc"]
    assert row["items"] == 5
    assert row["scored"] == 5
    assert row["critical"] == 5, "the four purged articles are still on the books"
    assert row["mean"] == 8.9  # (4 × 9.0 + 8.5) / 5
    assert [i["articleId"] for i in row["crit"]] == [survivor]
    assert row["crit"][0]["reason"] == "this month"
    assert row["criticalTruncated"] is True, "1 of 5 shown"
    assert row["critExpired"] is False, "we can still show something, so this is not an expired row"


# ── the economy board is on the same path ────────────────────────────


def test_an_economy_axis_expires_the_same_way_a_watch_entity_does(client):
    """`rollup_monthly_econ` is the macro counterpart of
    `rollup_monthly_watch` and `_board_row` treats them identically —
    which is worth pinning, because the two rollups are written by two
    separate functions (`_upsert_econ`, `_upsert_watch`) and only the
    watch one has ever been read after a purge.
    """
    conn = client.conn
    for score in (9.0, 8.0, 5.0):
        _insert_article(conn, month=LAST_MONTH, title=BANKING, score=score)
    conn.commit()
    _compute_rollups(conn, LAST_MONTH)
    _purge_before(conn, THIS_MONTH)

    banking = _econ_rows(client)["banking"]
    assert (banking["items"], banking["scored"], banking["critical"]) == (3, 3, 2)
    assert banking["mean"] == 7.3  # 22.0 / 3, rounded on the wire
    assert banking["crit"] == []
    assert banking["critExpired"] is True


# ── the risk tab ─────────────────────────────────────────────────────


def test_the_regulatory_line_survives_a_purge_and_what_moved_degrades_to_empty(client):
    """Two readers of the same purged window, one durable and one not.

    `risklines._regulatory_line` reads `rollup_monthly_watch` in a single
    query and must still report its critical count. "What moved this
    week" reads live `articles` only and must come back empty rather than
    erroring — an absence the provenance panel is there to explain.
    """
    conn = client.conn
    for score in (9.0, 8.0, 4.0):
        _insert_article(conn, month=LAST_MONTH, title=BTRC, score=score)
    conn.commit()
    _compute_rollups(conn, LAST_MONTH)

    moved_before = client.get("/dashboard/long/risk").json()["moved"]
    assert len(moved_before) == 2

    _purge_before(conn, THIS_MONTH)

    payload = client.get("/dashboard/long/risk")
    assert payload.status_code == 200, payload.text
    body = payload.json()

    line = next(ln for ln in body["lines"] if ln["key"] == "regulatory")
    assert line["value"] == "2", "the count is in the rollup and the rollup is still there"
    assert line["unit"] == "critical"
    assert line["direction"] == "rising"
    assert "BTRC actions" in line["evidence"]

    assert body["moved"] == [], "these are live articles and they are gone"
    # The coverage rollup outlives the purge too, so the provenance line
    # still describes a month that was genuinely read.
    assert "3 articles read" in body["provenance"]["news"]


# ── the fallback for a row the rollup has never covered ──────────────


def test_a_custom_entity_falls_back_to_live_stats_and_stops_being_new_once_rolled_up(client):
    """A custom entity is rolled up like any other, and falls back to
    live `articles` only until it has been.

    `rollups._upsert_watch` interpolates its patterns, which is safe only
    for the frozen module constants in `app.db.watchlist`; a custom
    entity's patterns are admin input, so `_upsert_watch_custom` covers
    them in a second pass with BOUND parameters. Before that pass existed
    a custom entity never got a rollup row at all, which quietly exempted
    admin-added rows from the durable-count guarantee the whole board
    rests on.

    `isNew` is checked either side of the rollup: it means "no durable
    history behind this row yet", so it must clear once there is.
    """
    conn = client.conn
    created = client.post("/watchlist", json={"groupKey": "mfs", "label": "Upay", "enTerms": ["upay"]})
    assert created.status_code == 201, created.text

    for score in (9.0, 6.0):
        _insert_article(conn, month=THIS_MONTH, title="Upay raises its cash-out fee", score=score)
    conn.commit()
    _mark_monitored(conn, THIS_MONTH)

    # Before the WATCH rollup runs, the row falls back to live stats and
    # is new. This is the real state of an entity added after the last
    # nightly pass: the month is monitored, but nothing has rolled the
    # new entity up yet.
    row = _watch_rows(client, "wallets")["upay"]
    assert (row["items"], row["scored"], row["critical"], row["mean"]) == (2, 2, 1, 7.5)
    assert len(row["crit"]) == 1, "live articles, so the headline is there"
    assert row["critExpired"] is False
    assert row["isNew"] is True, "no durable history behind this row yet"

    # The rollup pass now covers it, so a durable row exists...
    _compute_rollups(conn, THIS_MONTH)
    stored = conn.execute(
        "SELECT article_count, scored_count, high_count, score_sum FROM rollup_monthly_watch "
        "WHERE entity = 'upay'"
    ).fetchone()
    assert stored is not None, "a custom entity must be rolled up like every other row"
    assert (stored["article_count"], stored["scored_count"], stored["high_count"]) == (2, 2, 1)

    # ...the row reads from it, and the badge clears.
    row = _watch_rows(client, "wallets")["upay"]
    assert (row["items"], row["scored"], row["critical"], row["mean"]) == (2, 2, 1, 7.5)
    assert row["isNew"] is False


def test_a_custom_entity_keeps_its_counts_after_a_purge_like_every_other_row(client):
    """The same core case as the first test in this file, for a row the
    operator added instead of one the code ships.

    Nothing on the screen distinguishes the two — same board, same
    columns, same `critExpired` note — so a reader has no way to know
    that one of these rows forgets its history after 90 days and the
    others do not.
    """
    conn = client.conn
    created = client.post("/watchlist", json={"groupKey": "mfs", "label": "Upay", "enTerms": ["upay"]})
    assert created.status_code == 201, created.text

    for score in (9.0, 8.0, 4.0):
        _insert_article(conn, month=LAST_MONTH, title="Upay raises its cash-out fee", score=score)
    conn.commit()
    _compute_rollups(conn, LAST_MONTH)

    before = _watch_rows(client, "wallets")["upay"]
    assert (before["items"], before["critical"]) == (3, 2)

    _purge_before(conn, THIS_MONTH)

    after = _watch_rows(client, "wallets")["upay"]
    assert after["items"] == 3
    assert after["critical"] == 2
    assert after["critExpired"] is True


# ── ordering ─────────────────────────────────────────────────────────


def test_a_fully_expired_row_sorts_by_its_rollup_mean_not_to_the_bottom(client):
    """`_sorted_rows` puts unscored rows last, and an expired row is not
    an unscored one.

    Its `mean` comes from the rollup and survives the purge, so the
    sharpest axis in the window has to stay at the top of the board even
    once its headlines are gone. Dropping it to the bottom would bury the
    row a risk officer most needs, for the reason `_sorted_rows` exists
    to prevent.
    """
    conn = client.conn
    _insert_article(conn, month=LAST_MONTH, title=BTRC, score=9.0)
    _insert_article(conn, month=THIS_MONTH, title=SPECTRUM, score=5.0)
    conn.commit()
    _compute_rollups(conn, LAST_MONTH, THIS_MONTH)
    _purge_before(conn, THIS_MONTH)

    rows = client.get("/dashboard/long/telecom").json()["regulatory"]["rows"]
    keys = [r["key"] for r in rows]
    by_key = {r["key"]: r for r in rows}

    assert by_key["btrc"]["critExpired"] is True
    assert by_key["btrc"]["mean"] == 9.0
    assert by_key["spectrum"]["mean"] == 5.0
    assert keys[0] == "btrc", "highest mean first, expired or not"
    assert keys.index("btrc") < keys.index("spectrum")
    assert keys[-1] != "btrc"
    # And the rows with no reading at all are still the ones at the back.
    assert [r["mean"] for r in rows if r["mean"] is None] == [r["mean"] for r in rows[2:]]


# ── the one way the durable counts can still be lost ─────────────────


def test_recomputing_a_purged_month_destroys_the_counts_it_was_meant_to_preserve(client):
    """CHARACTERISATION, not an endorsement — the hazard at the edge of
    the design claim, pinned so a change to it is visible.

    `rollups.compute_rollups_for_month` is an absolute replace computed
    from live `articles` (its own docstring: "UPSERT the result as an
    absolute replace"). Run it over a month whose articles have been
    purged and every count for that month goes to zero, after which the
    board reports the genuine risk event as a quiet month — exactly the
    failure `_rollup_stats` exists to prevent.

    Production does not normally reach this: `run_daily` calls
    `compute_rollups_for_day(today)`, which only touches the current
    month, and `backfill_all_days` iterates the months still present in
    `articles`, which a purged month is not. It IS reachable by purging
    inside the current month, or by any hand-run backfill of an old one,
    and nothing in the purge path warns about it.
    """
    conn = client.conn
    for score in (9.0, 8.0, 4.0):
        _insert_article(conn, month=LAST_MONTH, title=BTRC, score=score)
    conn.commit()
    _compute_rollups(conn, LAST_MONTH)
    _purge_before(conn, THIS_MONTH)

    assert _watch_rows(client, "regulatory")["btrc"]["critical"] == 2

    rollups.compute_rollups_for_month(conn, LAST_MONTH)
    conn.commit()

    zeroed = conn.execute(
        "SELECT article_count, high_count FROM rollup_monthly_watch WHERE entity = 'btrc' AND month = %s",
        (LAST_MONTH,),
    ).fetchone()
    assert zeroed is not None
    assert (int(zeroed["article_count"]), int(zeroed["high_count"])) == (0, 0)

    # The coverage row is zeroed with it, so the month stops being
    # monitored and the whole board goes to zero — the count and the
    # window it was read through are lost together.
    row = _watch_rows(client, "regulatory")["btrc"]
    assert row["critical"] == 0
    assert row["critExpired"] is False, "the row now reads as a quiet month, which it was not"

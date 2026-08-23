"""CONCURRENCY and SUSTAINED LOAD for the rebuilt Long-term dashboard
(`app/api/routers/dashboard_long.py`).

WHY THIS FILE EXISTS SEPARATELY FROM ITS TWO SIBLINGS
─────────────────────────────────────────────────────
`tests/test_dashboard_long.py` and `tests/test_dashboard_long_edges.py`
both pin ONE psycopg connection into the router:

    monkeypatch.setattr(mod, "get_connection", lambda: nullcontext(conn))

That is the right call for what they test — it makes a test's own
`INSERT` visible to the endpoint without a commit dance — but it also
means every request in those 85 tests runs on the same connection, in
the same transaction, one at a time. No interleaving of any kind is
reachable from there. This file fills that gap: the app opens a REAL
connection per request (`app.db.connection.get_connection`, unmodified),
against a real `migrated_schema()` database, driven from real threads.

HOW THE APP IS POINTED AT THE TEST SCHEMA
─────────────────────────────────────────
`migrated_schema()` hands out a connection pinned to a throwaway schema
via `options=-c search_path=...`. To let the app's OWN `get_connection()`
reach that same schema, the `live` fixture puts the identical option into
`DATABASE_URL` as a libpq URI parameter and clears the settings cache.
Nothing in `app/` is monkeypatched to achieve it, so what runs here is
the production connection lifecycle: connect, commit or rollback, close,
once per request.

Two consequences worth knowing before reading any test below:

  * `migrated_schema()` yields a schema whose tables are NOT COMMITTED.
    `app.db.migrate._run_migrations` calls `applied_migrations(conn)`
    before its loop, which opens an implicit transaction, so every
    `with conn.transaction():` inside the loop is a SAVEPOINT rather than
    a top-level commit. The 85 sibling tests never notice because they
    share that one connection. Any SECOND connection sees an empty
    schema, so the fixture commits once immediately after migrating.
    This is a property of the test helper, not a production defect —
    `run_migrations()` with no connection argument uses
    `get_connection()`, which commits on exit.
  * The general rate limiter (`app/middleware/rate_limit.py`, 120
    requests / 60s / IP) is raised for these tests only. A sustained-load
    test that issues 200+ requests from one client address otherwise
    measures the limiter rather than the dashboard. The limiter itself is
    already covered by `tests/test_harden_rate_limit.py`; the ceiling is
    noted here because it IS the real sustained-request ceiling for a
    single browser on the deployed instance.

WHAT WAS FOUND
──────────────
One defect, in two shapes, both encoded as `xfail(strict=True)`:

  * `test_a_create_committed_mid_read_leaves_the_payload_self_consistent`
  * `test_a_delete_committed_mid_read_leaves_the_payload_self_consistent`

`GET /dashboard/long/telecom` (dashboard_long.py:650-716) reads the
watch-entity list SIX separate times on one connection — once per board
at :653-655, once for the initiatives loop at :668, once via
`watchlist_admin.list_custom` at :694 and once more for the payload's
`watchlist.entities` at :696. Under READ COMMITTED every one of those
statements takes a fresh snapshot, so a `POST /watchlist` or
`DELETE /watchlist/{id}` that commits between the first and the last
produces a payload that contradicts itself: an entity listed in
`watchlist.entities` with no row on the board it belongs to, or a board
row for an entity the same payload says does not exist. Observed and
expected are pinned in each test's docstring.

No socket is opened: `tests/conftest.py` blocks them process-wide, and
every macro/market fetcher is monkeypatched exactly as the sibling
files' `offline_macro` does. psycopg reaches Postgres through libpq,
which never touches the patched `socket.socket.connect`.
"""

from __future__ import annotations

import itertools
import os
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.api.routers.dashboard_long import CRIT_PER_ROW
from app.config import get_settings
from app.db import watchlist as watchlist_db
from app.db.rollups import compute_rollups_for_month
from app.db.testing import _candidate_dsn, migrated_schema, pg_available
from app.main import create_app
from app.services import macro as macro_service

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

# Captured at import, BEFORE any fixture rewrites DATABASE_URL to the
# schema-pinned form — otherwise a second test in the same process would
# build a DSN with two `options=` parameters.
BASE_DSN = _candidate_dsn()

ADMIN = AuthenticatedUser(id=1, email="cro@grameenphone.com", role="admin", display_name="CRO", session_id=1)

TABS = ("risk", "market", "economy", "telecom")

THIS_MONTH = date.today().replace(day=1)
LAST_MONTH = (THIS_MONTH - timedelta(days=1)).replace(day=1)

_url_seq = itertools.count()

# Every board row the endpoint must return once the watch groups are
# loaded. A payload with fewer rows on any board is a partially-populated
# board, which is one of the things concurrent reads must never produce.
GROUP_ROW_COUNTS = {g.key: len(g.entities) for g in watchlist_db.WATCH_GROUPS}
ECON_AXIS_COUNT = 6

# Where a p95 stops being acceptable for a screen a CRO opens tab by tab.
# Reported against, never asserted against — see LOAD_CEILING_SECONDS.
P95_TARGET_SECONDS = 1.5

# What the sustained-load test actually fails on. Deliberately far above
# every observed number (see the test's own report line) so a loaded CI
# box cannot turn a latency observation into a red suite.
LOAD_CEILING_SECONDS = 6.0

# How long any thread in this file may block before the test gives up and
# says so. Generous: these are correctness tests, not timing tests, and a
# hang must surface as a readable failure rather than as a stuck run.
JOIN_TIMEOUT_SECONDS = 120.0
CONDITION_TIMEOUT_SECONDS = 30.0


# ═════════════════════════════════════════════════════════════════════
# Harness
# ═════════════════════════════════════════════════════════════════════


def _schema_dsn(base: str, schema: str, app_name: str) -> str:
    """`base`, plus the same `search_path` pin `migrated_schema()` uses,
    plus a per-test `application_name` so `pg_stat_activity` can be
    filtered down to exactly the connections this app opened.
    """
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}options=-c%20search_path%3D{schema}&application_name={app_name}"


@dataclass
class LiveApp:
    """One migrated schema, one TestClient, and the instruments needed to
    watch what the app does to the database while the test drives it.
    """

    client: TestClient
    conn: Any
    schema: str
    dsn: str
    watcher: Any
    _extra: list[Any] = field(default_factory=list)

    def connect(self) -> Any:
        """A raw connection into the test schema, closed by the fixture.

        Used where a test has to hold a transaction open on purpose — the
        forced write race below — rather than go through the app.
        """
        conn = psycopg.connect(self.dsn, row_factory=dict_row)
        self._extra.append(conn)
        return conn

    def open_connections(self) -> int:
        """How many connections the APP currently holds open. Filtered by
        `application_name`, which only the DSN the app was handed sets,
        so the fixture's own connections are never counted.
        """
        row = self.watcher.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE application_name = %s",
            (self.schema,),
        ).fetchone()
        return int(row["n"])

    def lock_waiters(self) -> int:
        """How many of the app's connections are blocked waiting on a
        lock right now. This is the observable the forced write race
        synchronises on, instead of a sleep.
        """
        row = self.watcher.execute(
            "SELECT count(*) AS n FROM pg_stat_activity "
            "WHERE application_name = %s AND wait_event_type = 'Lock'",
            (self.schema,),
        ).fetchone()
        return int(row["n"])


def _wait_for(predicate: Callable[[], bool], *, timeout: float = CONDITION_TIMEOUT_SECONDS) -> bool:
    """Poll `predicate` until it holds or `timeout` expires.

    Polling a condition is not the sleep-based synchronisation the brief
    rules out: nothing here waits a fixed interval and hopes. Every call
    site treats a False return as a reportable fact.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


@pytest.fixture
def offline_macro(monkeypatch: pytest.MonkeyPatch):
    """No third party is reachable from a test, and none should be.

    Identical to the sibling suites' fixture. It matters more here: these
    tests run the fetchers from many threads at once, and a real one
    would be both a socket (blocked) and a source of timing noise in the
    latency table.
    """
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(macro_service, "fetch_usd_bdt", lambda: macro_service.MacroValue(122.0, now))
    monkeypatch.setattr(macro_service, "fetch_brent", lambda key: macro_service.MacroValue(93.6, now))
    monkeypatch.setattr(macro_service, "fetch_inflation", lambda: macro_service.MacroValue(8.77, now))
    monkeypatch.setattr(
        macro_service, "fetch_annual_exchange_rate", lambda: macro_service.AnnualValue(121.92, 2025, now)
    )


@pytest.fixture
def live(offline_macro) -> Iterator[LiveApp]:
    """The app, wired to a real per-request connection pool.

    Deliberately does NOT monkeypatch `dashboard_long.get_connection`.
    The whole point of this file is that every request opens, uses and
    closes its own connection exactly as it does in production.
    """
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "RATE_LIMIT_REQUESTS_PER_WINDOW")}
    with migrated_schema() as conn:
        # See this module's docstring: run_migrations leaves the DDL
        # inside one uncommitted transaction when handed a connection
        # that has already executed a statement.
        conn.commit()
        schema = conn.execute("SELECT current_schema() AS s").fetchone()["s"]
        conn.commit()

        watcher = psycopg.connect(BASE_DSN, row_factory=dict_row, autocommit=True)
        app_obj = LiveApp(client=None, conn=conn, schema=schema, dsn="", watcher=watcher)  # type: ignore[arg-type]
        try:
            dsn = _schema_dsn(BASE_DSN, schema, schema)
            app_obj.dsn = dsn
            os.environ["DATABASE_URL"] = dsn
            os.environ["RATE_LIMIT_REQUESTS_PER_WINDOW"] = "1000000"
            get_settings.cache_clear()

            app = create_app()
            app.dependency_overrides[get_current_user] = lambda: ADMIN
            app.dependency_overrides[require_admin] = lambda: ADMIN
            with TestClient(app) as client:
                app_obj.client = client
                yield app_obj
        finally:
            for extra in app_obj._extra:
                extra.close()
            watcher.close()
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            get_settings.cache_clear()


# ═════════════════════════════════════════════════════════════════════
# Seeding
# ═════════════════════════════════════════════════════════════════════

# One subject per code-defined watch entity plus a few economy cues, so
# every board comes back populated rather than a page of empty rows.
SUBJECTS = (
    "Grameenphone",
    "Robi Axiata",
    "Banglalink",
    "Teletalk",
    "Starlink",
    "Airtel",
    "BTRC",
    "spectrum auction",
    "telecom tax and VAT",
    "SIM registration rules",
    "call drop service quality",
    "internet shutdown",
    "submarine cable bandwidth",
    "bKash mobile banking",
    "Nagad wallet payment",
    "load shedding and power tariff",
    "RMG export earnings",
    "dollar reserve and remittance",
    "Bangladesh Bank and NPL",
    "NBR revenue and tax",
    "DSE index and IPO",
)


def _articles(count: int, months: tuple[date, ...]) -> list[dict[str, Any]]:
    """A realistic corpus: every subject represented, a third unscored,
    the rest spread across the whole 1-10 rubric.

    Scores are held inside 1.0-10.0 on purpose — `mean` outside that band
    is one of the invariants the concurrent readers assert, and a seed
    that could produce a 0.0 would make the assertion meaningless.
    """
    rows: list[dict[str, Any]] = []
    for i in range(count):
        month = months[i % len(months)]
        day_of_month = 1 + (i % 20)
        scored = i % 3 != 0
        rows.append(
            {
                "url": f"https://x.test/c-{next(_url_seq)}",
                "url_norm": "",
                "source": "Prothom Alo",
                "title": f"{SUBJECTS[i % len(SUBJECTS)]} story {i}",
                "title_en": "",
                "day": month + timedelta(days=day_of_month - 1),
                # Inside the SAME calendar month as `day`: rollups derive
                # `collected_days` from fetched_at, so a fetched_at of
                # "now" would leave last month with zero collection days
                # and drop it out of the monitored window entirely.
                "fetched_at": datetime(month.year, month.month, day_of_month, 9, 0, tzinfo=timezone.utc),
                "sections": "Telecom",
                "keywords": "",
                "review_status": "pending",
                "llm_score": (1.0 + (i % 10)) if scored else None,
                "llm_reason": "seeded reason" if scored else "",
            }
        )
    return rows


def _insert_articles(conn: Any, rows: list[dict[str, Any]]) -> None:
    cols = list(rows[0])
    sql = (
        f"INSERT INTO articles ({', '.join(cols)}) "
        f"VALUES ({', '.join('%(' + c + ')s' for c in cols)})"
    )
    with conn.cursor() as cur:
        cur.executemany(sql, rows)


def _seed(conn: Any, *, count: int, months: tuple[date, ...] = (LAST_MONTH, THIS_MONTH)) -> None:
    """Articles, rollups and market history, committed.

    `compute_rollups_for_month` is the real thing (app/db/rollups.py),
    called rather than faked: the boards read `rollup_monthly_watch` and
    `rollup_monthly_econ` for their counts, so hand-written rollup rows
    would test a path the product does not take.
    """
    _insert_articles(conn, _articles(count, months))
    conn.commit()
    for month in months:
        compute_rollups_for_month(conn, month)
    conn.commit()

    for code, base in (("GP", 300.0), ("ROBI", 30.0)):
        for i in range(12):
            close_month = THIS_MONTH - timedelta(days=30 * (11 - i))
            conn.execute(
                "INSERT INTO market_daily_close (code, day, close_price) VALUES (%s, %s, %s) "
                "ON CONFLICT (code, day) DO UPDATE SET close_price = EXCLUDED.close_price",
                (code, close_month.replace(day=15), base - i),
            )
        # Every numeric column here is NOT NULL by design (migration 0024:
        # a half-parsed quote is discarded upstream rather than stored), so
        # the seed has to be a whole quote even though these tests only
        # ever read `last_price` and `as_of`.
        conn.execute(
            "INSERT INTO market_quote "
            "(code, name, last_price, change_pct, day_low, day_high, "
            " week52_low, week52_high, market_cap_mn, as_of) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (code) DO NOTHING",
            (
                code,
                f"{code} Ltd.",
                base,
                0.5,
                base - 5,
                base + 5,
                base - 40,
                base + 40,
                base * 1000,
                THIS_MONTH,
            ),
        )
    conn.commit()


# ═════════════════════════════════════════════════════════════════════
# Invariants
# ═════════════════════════════════════════════════════════════════════


def _board_complaints(board: dict[str, Any], *, expected_rows: int) -> list[str]:
    """Everything that must be true of one board, whoever else is reading
    or writing at the same time. Returns complaints rather than
    asserting, so a concurrent test can report every payload that broke
    instead of only the first.
    """
    out: list[str] = []
    rows = board["rows"]
    keys = [r["key"] for r in rows]
    if len(keys) != expected_rows:
        out.append(f"board {board['key']}: {len(keys)} rows, expected {expected_rows} (partially populated)")
    if len(keys) != len(set(keys)):
        duplicated = sorted({k for k in keys if keys.count(k) > 1})
        out.append(f"board {board['key']}: duplicate row keys {duplicated}")
    for row in rows:
        where = f"board {board['key']} row {row['key']}"
        if len(row["crit"]) > row["critical"]:
            out.append(f"{where}: {len(row['crit'])} crit items but critical={row['critical']}")
        if len(row["crit"]) > CRIT_PER_ROW:
            out.append(f"{where}: {len(row['crit'])} crit items, cap is {CRIT_PER_ROW}")
        if row["mean"] is not None and not (1.0 <= row["mean"] <= 10.0):
            out.append(f"{where}: mean {row['mean']} outside the 1-10 rubric")
        if row["scored"] > row["items"]:
            out.append(f"{where}: scored={row['scored']} exceeds items={row['items']}")
        if row["critical"] > row["scored"]:
            out.append(f"{where}: critical={row['critical']} exceeds scored={row['scored']}")
        if row["mean"] is None and row["scored"]:
            out.append(f"{where}: scored={row['scored']} but no mean")
    return out


def _telecom_complaints(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name, group_key in (("peers", "operators"), ("wallets", "mfs"), ("regulatory", "regulatory")):
        out.extend(_board_complaints(payload[name], expected_rows=GROUP_ROW_COUNTS[group_key]))
    return out


def _payload_complaints(tab: str, payload: dict[str, Any]) -> list[str]:
    if tab == "telecom":
        return _telecom_complaints(payload)
    if tab == "economy":
        return _board_complaints(payload["board"], expected_rows=ECON_AXIS_COUNT)
    if tab == "risk":
        # Every risk line must be present and carry a direction; a
        # half-built line list is the "partially-populated board" failure
        # in this tab's shape.
        return (
            []
            if payload["lines"] and all(ln.get("direction") for ln in payload["lines"])
            else [f"risk: {len(payload['lines'])} lines, directions {[ln.get('direction') for ln in payload['lines']]}"]
        )
    return []


def _self_consistency_complaints(payload: dict[str, Any]) -> list[str]:
    """The invariant the brief names: an entity in `watchlist.entities`
    must have a board row, and every board row must be an entity the
    same payload lists.
    """
    out: list[str] = []
    board_keys = {
        payload[name]["key"]: {r["key"] for r in payload[name]["rows"]}
        for name in ("peers", "wallets", "regulatory")
    }
    listed: set[tuple[str, str]] = set()
    for entity in payload["watchlist"]["entities"]:
        listed.add((entity["groupKey"], entity["key"]))
        keys = board_keys.get(entity["groupKey"])
        if keys is None:
            out.append(f"entity {entity['key']} names board {entity['groupKey']}, which the payload has no board for")
        elif entity["key"] not in keys:
            out.append(
                f"entity {entity['key']} is in watchlist.entities but has no row on the "
                f"{entity['groupKey']} board"
            )
    for group_key, keys in board_keys.items():
        for key in sorted(keys):
            if (group_key, key) not in listed:
                out.append(f"the {group_key} board has a row {key!r} that watchlist.entities does not list")
    return out


# ═════════════════════════════════════════════════════════════════════
# 1. Real concurrent reads
# ═════════════════════════════════════════════════════════════════════


def _drive(client: TestClient, tab: str, rounds: int, barrier: threading.Barrier) -> list[tuple[int, Any]]:
    barrier.wait(timeout=JOIN_TIMEOUT_SECONDS)
    out: list[tuple[int, Any]] = []
    for _ in range(rounds):
        response = client.get(f"/dashboard/long/{tab}")
        out.append((response.status_code, response.json() if response.status_code == 200 else response.text))
    return out


def test_the_four_tabs_answer_coherently_under_eight_concurrent_readers(live: LiveApp):
    """Eight threads, two per tab, twelve reads each — 96 requests, each
    on its own connection, all released at one barrier.

    Every response must be a 200 whose board is fully populated, whose
    rows are unique, whose `crit` list never outruns its own `critical`
    count and whose `mean` stays inside the rubric. Nothing here is
    written while the readers run, so any violation would be the endpoint
    contradicting itself rather than a torn read.
    """
    _seed(live.conn, count=600)

    barrier = threading.Barrier(8)
    plan = [(tab, 12) for tab in TABS for _ in range(2)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_drive, live.client, tab, rounds, barrier) for tab, rounds in plan]
        results = [f.result(timeout=JOIN_TIMEOUT_SECONDS) for f in futures]

    complaints: list[str] = []
    statuses: list[int] = []
    for (tab, _rounds), outcomes in zip(plan, results):
        for status, payload in outcomes:
            statuses.append(status)
            if status != 200:
                complaints.append(f"{tab}: HTTP {status} — {str(payload)[:200]}")
                continue
            complaints.extend(f"{tab}: {c}" for c in _payload_complaints(tab, payload))

    assert len(statuses) == 96, f"expected 96 responses, got {len(statuses)}"
    assert not complaints, "concurrent readers saw an incoherent dashboard:\n  " + "\n  ".join(complaints[:20])


def test_concurrent_readers_really_do_overlap_rather_than_queue(live: LiveApp):
    """The guard on the test above.

    If the harness serialised requests — one connection reused, or a lock
    somewhere in the stack — every assertion in this file about
    concurrency would be vacuous. So watch `pg_stat_activity` while eight
    readers run and require that the app genuinely held more than one
    connection open at the same moment.
    """
    _seed(live.conn, count=300)

    peak = [0]
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], live.open_connections())
            time.sleep(0.002)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    barrier = threading.Barrier(8)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_drive, live.client, tab, 8, barrier) for tab in TABS for _ in range(2)]
            for future in futures:
                assert all(status == 200 for status, _ in future.result(timeout=JOIN_TIMEOUT_SECONDS))
    finally:
        stop.set()
        sampler.join(timeout=JOIN_TIMEOUT_SECONDS)

    assert peak[0] >= 2, (
        f"peak concurrent app connections was {peak[0]} — the readers never overlapped, so every other "
        "concurrency assertion in this file would be testing nothing"
    )


# ═════════════════════════════════════════════════════════════════════
# 2. Concurrent writes against reads
# ═════════════════════════════════════════════════════════════════════


def test_a_reader_never_500s_while_entities_are_created_and_deleted(live: LiveApp):
    """One thread adds and removes custom watchlist entities; another
    reads the Telecom tab in a loop on its own connection.

    The reader's contract under write traffic is narrow and absolute: it
    answers 200 every time. Payload self-consistency is NOT asserted here
    — see the two deterministic tests below, which pin that separately
    and would flake if asserted against a real race.
    """
    _seed(live.conn, count=300)

    stop = threading.Event()
    writer_errors: list[str] = []
    created_labels: list[str] = []

    def write() -> None:
        for i in range(12):
            label = f"racefree{i}"
            response = live.client.post(
                "/watchlist", json={"groupKey": "mfs", "label": label, "enTerms": [f"racefree{i}"]}
            )
            if response.status_code != 201:
                writer_errors.append(f"create {label}: HTTP {response.status_code} — {response.text[:200]}")
                continue
            created_labels.append(label)
            deleted = live.client.delete(f"/watchlist/{response.json()['id']}")
            if deleted.status_code != 204:
                writer_errors.append(f"delete {label}: HTTP {deleted.status_code} — {deleted.text[:200]}")
        stop.set()

    reads: list[tuple[int, Any]] = []
    inconsistent = 0

    def read() -> None:
        nonlocal inconsistent
        while not stop.is_set():
            response = live.client.get("/dashboard/long/telecom")
            body = response.json() if response.status_code == 200 else response.text
            reads.append((response.status_code, None))
            if response.status_code == 200:
                if _self_consistency_complaints(body):
                    inconsistent += 1
            else:
                reads[-1] = (response.status_code, str(body)[:200])

    writer = threading.Thread(target=write)
    reader = threading.Thread(target=read)
    reader.start()
    writer.start()
    writer.join(timeout=JOIN_TIMEOUT_SECONDS)
    stop.set()
    reader.join(timeout=JOIN_TIMEOUT_SECONDS)
    assert not writer.is_alive() and not reader.is_alive(), "a worker thread did not finish"

    assert not writer_errors, "the writer failed:\n  " + "\n  ".join(writer_errors)
    assert len(created_labels) == 12
    assert reads, "the reader never got a request in"
    bad = [r for r in reads if r[0] != 200]
    assert not bad, f"{len(bad)} of {len(reads)} reads were not 200: {bad[:5]}"
    # Reported, not asserted: how often the race actually tore. The
    # deterministic tests below are what pin the defect.
    print(
        f"\n[writes-vs-reads] {len(reads)} reads during 12 create+delete cycles, "
        f"{inconsistent} of them internally inconsistent"
    )


def _write_after_nth_entity_read(
    monkeypatch: pytest.MonkeyPatch, n: int, action: Callable[[], None]
) -> dict[str, int]:
    """Commit `action` immediately after the Nth `all_entities_with_custom`
    call inside a request.

    This is how a genuine interleaving is made deterministic instead of
    hoped for. `GET /dashboard/long/telecom` calls that function once per
    board and again for the payload's entity list; firing the write after
    the FIRST call places the commit between the operators board
    (`dashboard_long.py:653`) and every later read on the same
    connection, which is exactly the window a real concurrent admin write
    lands in. Nothing in `app/` is modified — the real function still
    runs and still returns its real result.
    """
    real = watchlist_db.all_entities_with_custom
    state = {"calls": 0}

    def wrapper(conn: Any) -> Any:
        result = real(conn)
        state["calls"] += 1
        if state["calls"] == n:
            action()
        return result

    monkeypatch.setattr(watchlist_db, "all_entities_with_custom", wrapper)
    return state


@pytest.mark.xfail(
    strict=True,
    reason=(
        "dashboard_long.py:650-716 — GET /dashboard/long/telecom reads the watch-entity list six times on one "
        "READ COMMITTED connection. A POST /watchlist that commits after the operators board is built "
        "(:653) and before watchlist.entities is built (:696) returns a payload listing an entity that has "
        "no row on its own board."
    ),
)
def test_a_create_committed_mid_read_leaves_the_payload_self_consistent(
    live: LiveApp, monkeypatch: pytest.MonkeyPatch
):
    """DEFECT — torn read across `GET /dashboard/long/telecom`.

    Interleaving: a Telecom read builds the operators board from the
    entity list as it stands, then an admin's `POST /watchlist` for a new
    OPERATORS entity commits, then the same read builds
    `watchlist.entities` from a second, newer snapshot.

    Expected: one payload, one view of the world — every entity in
    `watchlist.entities` has a row on the board it names.
    Observed: `watchlist.entities` carries the new entity and the peers
    board does not, so the screen renders a watchlist chip for a row that
    is not on the board underneath it.
    """
    _seed(live.conn, count=200)
    created: list[int] = []

    def add_one() -> None:
        response = live.client.post(
            "/watchlist", json={"groupKey": "operators", "label": "midread", "enTerms": ["midread"]}
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])

    state = _write_after_nth_entity_read(monkeypatch, 1, add_one)

    response = live.client.get("/dashboard/long/telecom")
    assert response.status_code == 200, response.text
    assert created, "the mid-read create never fired"
    assert state["calls"] >= 2, f"only {state['calls']} entity reads in one request — the hook is in the wrong place"

    complaints = _self_consistency_complaints(response.json())
    assert not complaints, "the payload contradicts itself:\n  " + "\n  ".join(complaints)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "dashboard_long.py:650-716 — the mirror of the create case. A DELETE /watchlist/{id} that commits "
        "after the board at :653 and before watchlist.entities at :696 returns a board row for an entity the "
        "same payload says does not exist."
    ),
)
def test_a_delete_committed_mid_read_leaves_the_payload_self_consistent(
    live: LiveApp, monkeypatch: pytest.MonkeyPatch
):
    """DEFECT — the same torn read, in the other direction.

    Interleaving: a custom operators entity exists; a Telecom read builds
    the peers board including it; the admin's `DELETE /watchlist/{id}`
    commits; the same read builds `watchlist.entities` without it.

    Expected: every board row is an entity the payload also lists.
    Observed: the peers board keeps a row for the deleted entity while
    `watchlist.entities` has already dropped it — a row the screen cannot
    open, explain or remove.
    """
    _seed(live.conn, count=200)
    created = live.client.post(
        "/watchlist", json={"groupKey": "operators", "label": "doomed", "enTerms": ["doomed"]}
    )
    assert created.status_code == 201, created.text
    entity_id = created.json()["id"]

    def delete_one() -> None:
        response = live.client.delete(f"/watchlist/{entity_id}")
        assert response.status_code == 204, response.text

    state = _write_after_nth_entity_read(monkeypatch, 1, delete_one)

    response = live.client.get("/dashboard/long/telecom")
    assert response.status_code == 200, response.text
    assert state["calls"] >= 2

    complaints = _self_consistency_complaints(response.json())
    assert not complaints, "the payload contradicts itself:\n  " + "\n  ".join(complaints)


# ═════════════════════════════════════════════════════════════════════
# 3. Two writers, one label
# ═════════════════════════════════════════════════════════════════════


def _post_label(client: TestClient, label: str, barrier: threading.Barrier) -> tuple[int, str]:
    barrier.wait(timeout=JOIN_TIMEOUT_SECONDS)
    response = client.post("/watchlist", json={"groupKey": "mfs", "label": label, "enTerms": [label]})
    return response.status_code, response.text


def test_two_writers_racing_for_one_label_leave_exactly_one_row(live: LiveApp):
    """`watch_entities.key` is UNIQUE table-wide, so of two simultaneous
    creates exactly one may win — and the loser owes the operator an
    explanation, not a 500.

    Six rounds, each with its own label, both requests released together
    from a barrier so the two INSERTs go out inside the same few
    microseconds.
    """
    outcomes: list[tuple[str, int, int]] = []
    for round_no in range(6):
        label = f"raceround{round_no}"
        barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_post_label, live.client, label, barrier) for _ in range(2)]
            pairs = [f.result(timeout=JOIN_TIMEOUT_SECONDS) for f in futures]

        statuses = sorted(status for status, _ in pairs)
        bodies = [body for _status, body in pairs]
        live.conn.commit()
        rows = live.conn.execute(
            "SELECT count(*) AS n FROM watch_entities WHERE key = %s", (label,)
        ).fetchone()["n"]
        outcomes.append((label, statuses[0], rows))

        assert 500 not in statuses, f"{label}: a race produced a 500 — {bodies}"
        assert statuses.count(201) == 1, f"{label}: expected exactly one winner, got statuses {statuses} — {bodies}"
        loser = [s for s in statuses if s != 201][0]
        assert 400 <= loser < 500, f"{label}: the loser got {loser}, not a clean 4xx — {bodies}"
        assert rows == 1, f"{label}: {rows} rows in watch_entities, expected exactly 1"

    print(f"\n[label race] 6 rounds, loser statuses {sorted({o[1] for o in outcomes})}, one row each round")


def test_a_forced_write_race_gives_the_loser_a_clean_4xx(live: LiveApp):
    """The race above, made deterministic — and proved to be a race.

    A third connection INSERTs the contested key and holds the
    transaction open, so BOTH endpoint requests provably block on the
    same unique index (observed in `pg_stat_activity.wait_event_type =
    'Lock'` before anything is released). The holder then rolls back,
    leaving Postgres — not the test's timing — to pick a winner.

    This is the interleaving `test_two_writers_racing_for_one_label...`
    can only hope for: both writers are genuinely inside their INSERT at
    the same instant.
    """
    label = "forcedrace"
    holder = live.connect()
    holder.execute(
        "INSERT INTO watch_entities (group_key, key, label, note, created_by) VALUES (%s, %s, %s, %s, %s)",
        ("mfs", label, label, "", "holder@test"),
    )

    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_post_label, live.client, label, barrier) for _ in range(2)]

        both_blocked = _wait_for(lambda: live.lock_waiters() >= 2)
        holder.rollback()

        pairs = [f.result(timeout=JOIN_TIMEOUT_SECONDS) for f in futures]

    statuses = sorted(status for status, _ in pairs)
    bodies = [body for _status, body in pairs]
    live.conn.commit()
    rows = live.conn.execute("SELECT count(*) AS n FROM watch_entities WHERE key = %s", (label,)).fetchone()["n"]

    assert both_blocked, (
        "never observed two app connections waiting on a lock — the two writers did not actually overlap, "
        "so this test proved nothing"
    )
    assert 500 not in statuses, f"a forced race produced a 500 — {bodies}"
    assert statuses.count(201) == 1, f"expected exactly one winner, got {statuses} — {bodies}"
    loser = [s for s in statuses if s != 201][0]
    assert 400 <= loser < 500, f"the loser got {loser}, not a clean 4xx — {bodies}"
    assert rows == 1, f"{rows} rows in watch_entities, expected exactly 1"


# ═════════════════════════════════════════════════════════════════════
# 4. Delete while reading
# ═════════════════════════════════════════════════════════════════════


def test_a_delete_between_the_start_and_the_end_of_a_board_read_never_raises(
    live: LiveApp, monkeypatch: pytest.MonkeyPatch
):
    """The entity a board row is being built from is deleted underneath
    the request.

    The delete is committed at a known point inside the read (right after
    the first entity-list read), so the rest of the request works from a
    list whose rows no longer exist in the database. It must still
    answer: patterns are already in memory, and every later statement is
    a plain read. A 500 here would mean the endpoint depends on rows
    staying put for the length of a request, which nothing guarantees.
    """
    _seed(live.conn, count=200)
    created = live.client.post(
        "/watchlist", json={"groupKey": "mfs", "label": "vanishing", "enTerms": ["vanishing"]}
    )
    assert created.status_code == 201, created.text
    entity_id = created.json()["id"]

    fired: list[int] = []

    def delete_mid_read() -> None:
        response = live.client.delete(f"/watchlist/{entity_id}")
        fired.append(response.status_code)

    state = _write_after_nth_entity_read(monkeypatch, 1, delete_mid_read)

    response = live.client.get("/dashboard/long/telecom")
    assert fired == [204], f"the mid-read delete did not run cleanly: {fired}"
    assert state["calls"] >= 2
    assert response.status_code == 200, response.text
    # And the boards it did manage to build are still well-formed.
    payload = response.json()
    for name, group_key in (("peers", "operators"), ("regulatory", "regulatory")):
        assert not _board_complaints(payload[name], expected_rows=GROUP_ROW_COUNTS[group_key])


def test_deleting_an_entity_under_a_loop_of_readers_never_breaks_a_read(live: LiveApp):
    """The same thing without a hook: three readers loop over the Telecom
    tab while twenty custom entities are deleted one by one.

    Unhooked, so the delete lands wherever it lands — which over hundreds
    of reads covers every point in the request. Every read must be a 200.
    """
    _seed(live.conn, count=200)
    ids: list[int] = []
    for i in range(20):
        response = live.client.post(
            "/watchlist", json={"groupKey": "mfs", "label": f"transient{i}", "enTerms": [f"transient{i}"]}
        )
        assert response.status_code == 201, response.text
        ids.append(response.json()["id"])

    stop = threading.Event()
    statuses: list[int] = []
    delete_statuses: list[int] = []

    def read() -> None:
        while not stop.is_set():
            statuses.append(live.client.get("/dashboard/long/telecom").status_code)

    def delete() -> None:
        for entity_id in ids:
            delete_statuses.append(live.client.delete(f"/watchlist/{entity_id}").status_code)
        stop.set()

    readers = [threading.Thread(target=read) for _ in range(3)]
    deleter = threading.Thread(target=delete)
    for reader in readers:
        reader.start()
    deleter.start()
    deleter.join(timeout=JOIN_TIMEOUT_SECONDS)
    stop.set()
    for reader in readers:
        reader.join(timeout=JOIN_TIMEOUT_SECONDS)
    assert not any(r.is_alive() for r in readers) and not deleter.is_alive()

    assert delete_statuses == [204] * 20, f"deletes: {delete_statuses}"
    assert statuses, "no read completed"
    assert set(statuses) == {200}, f"{sum(1 for s in statuses if s != 200)} of {len(statuses)} reads failed"
    print(f"\n[delete-while-reading] {len(statuses)} reads across 20 concurrent deletes, all 200")


# ═════════════════════════════════════════════════════════════════════
# 5. Sustained load
# ═════════════════════════════════════════════════════════════════════


def _percentiles(samples: list[float]) -> tuple[float, float, float]:
    ordered = sorted(samples)
    p50 = statistics.median(ordered)
    index = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
    return p50, ordered[index], ordered[-1]


def test_sustained_load_on_a_five_thousand_article_corpus(live: LiveApp):
    """Fifty reads of every tab against a realistic corpus, with the
    latency distribution reported rather than guessed at.

    The corpus is 5,000 articles across two monitored months WITH the
    real rollups computed for both (`app/db/rollups.py`), because the
    boards read `rollup_monthly_watch` / `rollup_monthly_econ` for their
    counts and touch live `articles` only for the three headlines each
    row carries. If the rollup path did not dominate, these numbers would
    grow with the corpus instead of with the number of board rows.

    Asserted against a ceiling far above anything observed
    (LOAD_CEILING_SECONDS); the real p50/p95/max are printed, and the
    P95_TARGET_SECONDS line is where a number stops being acceptable for
    this screen.
    """
    started = time.perf_counter()
    _seed(live.conn, count=5000)
    seed_seconds = time.perf_counter() - started

    total = live.conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]
    watch_rows = live.conn.execute("SELECT count(*) AS n FROM rollup_monthly_watch").fetchone()["n"]
    econ_rows = live.conn.execute("SELECT count(*) AS n FROM rollup_monthly_econ").fetchone()["n"]
    assert total == 5000, total
    assert watch_rows > 0 and econ_rows > 0, f"rollups did not materialise: watch={watch_rows} econ={econ_rows}"

    timings: dict[str, list[float]] = {tab: [] for tab in TABS}
    for _round in range(50):
        for tab in TABS:
            start = time.perf_counter()
            response = live.client.get(f"/dashboard/long/{tab}")
            timings[tab].append(time.perf_counter() - start)
            assert response.status_code == 200, f"{tab}: HTTP {response.status_code} — {response.text[:300]}"

    report_lines = [f"corpus 5,000 articles / 2 monitored months, seeded+rolled up in {seed_seconds:.1f}s"]
    over_target: list[str] = []
    over_ceiling: list[str] = []
    for tab in TABS:
        p50, p95, worst = _percentiles(timings[tab])
        report_lines.append(
            f"  {tab:<8} n=50  p50 {p50 * 1000:7.1f}ms  "
            f"p95 {p95 * 1000:7.1f}ms  max {worst * 1000:7.1f}ms"
        )
        if p95 > P95_TARGET_SECONDS:
            over_target.append(f"{tab} p95 {p95:.3f}s")
        if p95 > LOAD_CEILING_SECONDS:
            over_ceiling.append(f"{tab} p95 {p95:.3f}s")
    report = "\n".join(report_lines)
    print(f"\n[sustained load]\n{report}")
    if over_target:
        print(f"[sustained load] OVER the {P95_TARGET_SECONDS}s target: {', '.join(over_target)}")

    assert not over_ceiling, f"p95 over {LOAD_CEILING_SECONDS}s:\n{report}"


def test_sustained_load_holds_up_with_readers_on_every_tab_at_once(live: LiveApp):
    """The same corpus, read concurrently instead of in turn.

    Four threads, one per tab, fifteen reads each. Contention must not
    turn a working screen into a failing one, and the wall-clock total
    must stay inside the same generous ceiling per request.
    """
    _seed(live.conn, count=5000)

    barrier = threading.Barrier(4)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {tab: pool.submit(_drive, live.client, tab, 15, barrier) for tab in TABS}
        outcomes = {tab: f.result(timeout=JOIN_TIMEOUT_SECONDS) for tab, f in futures.items()}
    elapsed = time.perf_counter() - started

    complaints: list[str] = []
    for tab, results in outcomes.items():
        for status, payload in results:
            if status != 200:
                complaints.append(f"{tab}: HTTP {status} — {str(payload)[:200]}")
            else:
                complaints.extend(f"{tab}: {c}" for c in _payload_complaints(tab, payload))
    assert not complaints, "concurrent load broke the dashboard:\n  " + "\n  ".join(complaints[:20])

    per_request = elapsed / 60
    print(f"\n[concurrent load] 60 requests across 4 tabs in {elapsed:.2f}s ({per_request * 1000:.0f}ms/request mean)")
    assert per_request < LOAD_CEILING_SECONDS, f"{per_request:.3f}s mean per request under 4-way concurrency"


# ═════════════════════════════════════════════════════════════════════
# 6. Connection hygiene
# ═════════════════════════════════════════════════════════════════════


def test_a_burst_of_reads_leaves_no_connection_behind(live: LiveApp):
    """`app/db/connection.py` opens one connection per request and closes
    it in a `finally`. This is the test that the endpoints actually let
    it: baseline before, burst, baseline after.

    Counted from `pg_stat_activity` filtered on the per-test
    `application_name`, so nothing but the app's own request connections
    is in the number.
    """
    _seed(live.conn, count=300)
    assert _wait_for(lambda: live.open_connections() == 0), (
        f"{live.open_connections()} app connections open before the burst"
    )

    peak = [0]
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            peak[0] = max(peak[0], live.open_connections())
            time.sleep(0.002)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    barrier = threading.Barrier(8)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_drive, live.client, tab, 10, barrier) for tab in TABS for _ in range(2)]
            for future in futures:
                assert all(status == 200 for status, _ in future.result(timeout=JOIN_TIMEOUT_SECONDS))
    finally:
        stop.set()
        sampler.join(timeout=JOIN_TIMEOUT_SECONDS)

    settled = _wait_for(lambda: live.open_connections() == 0)
    print(f"\n[hygiene] 80 requests, peak {peak[0]} concurrent app connections, {live.open_connections()} left open")
    assert settled, f"{live.open_connections()} connections still open after 80 requests (peak was {peak[0]})"
    # The app has no pool, so the ceiling is "one per in-flight request".
    # Anything above the number of driving threads would mean a request
    # opened a second connection without closing the first.
    assert peak[0] <= 8, f"peak {peak[0]} connections for 8 concurrent requests"


def test_a_burst_of_failing_requests_leaves_no_connection_behind(live: LiveApp):
    """The error paths, which are where a connection actually gets
    leaked if it ever does.

    404 (delete a missing entity), 422 (a term the preview refuses, and a
    label that collides with a code-defined entity) — every one of those
    leaves the handler through `raise`, not `return`, and every one must
    still close its connection.
    """
    _seed(live.conn, count=200)
    assert _wait_for(lambda: live.open_connections() == 0)

    seen: dict[int, int] = {}
    for _ in range(25):
        for response in (
            live.client.delete("/watchlist/999999"),
            live.client.post("/watchlist/preview", json={"enTerms": ["x"], "groupKey": "mfs"}),
            live.client.post("/watchlist", json={"groupKey": "operators", "label": "Grameenphone", "enTerms": ["gp"]}),
            live.client.post("/watchlist/preview", json={"enTerms": [], "bnTerms": [], "groupKey": "mfs"}),
        ):
            seen[response.status_code] = seen.get(response.status_code, 0) + 1

    assert set(seen) <= {200, 404, 422}, f"unexpected statuses from the error paths: {seen}"
    assert seen.get(404) == 25 and seen.get(422, 0) >= 25, seen
    settled = _wait_for(lambda: live.open_connections() == 0)
    print(f"\n[hygiene/errors] 100 failing requests, statuses {seen}, {live.open_connections()} left open")
    assert settled, f"{live.open_connections()} connections still open after 100 failing requests"

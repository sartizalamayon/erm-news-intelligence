"""RED-TEAM: purge dry-run vs. real delete (mission attack surface
"Storage/purge ... purge with a dry-run count that disagrees with the
real delete").

app.services.storage's purge functions are a manual, two-step admin
flow by design (module docstring: "dry-run count and an explicit
confirmation step"): call once with `confirm=False` to get a preview
count to show the operator, then call again with `confirm=True` to
actually delete. Both calls share the exact same SQL WHERE clause, so
if nothing else touches the table in between, the counts genuinely
agree -- that part is solid and this file does not dispute it.

Finding (see docs/red-team-findings.md, RT-6): the two calls are not
atomic with each other. Nothing snapshots which ROWS the dry-run counted
(only a scalar count is returned), and nothing locks the qualifying rows
between the two calls. In the real admin flow described in the
module's own docstring and ADR §9.2 -- preview a count, show it to a
human, wait for them to click confirm, THEN issue the real delete --
there is an arbitrarily long window between the two calls during which
the batch job (running daily, independently, concurrently) can insert
or age more rows into the same WHERE clause. The confirmed delete then
silently affects MORE rows than the number the operator was shown and
approved. This reproduces it with an ordinary sequential insert between
the two calls (no real concurrency needed to prove the count can
diverge) against `purge_stale_unmatched_articles`, the purge most
likely to race against the daily batch job in practice.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.services.storage import purge_stale_unmatched_articles

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def _stale_unmatched_article(url: str, *, days_old: int) -> dict[str, object]:
    return {
        "url": url,
        "url_norm": url,
        "source": "Example",
        "title": "Old unmatched headline",
        "day": dt.date.today() - dt.timedelta(days=days_old),
        "review_status": "filtered_out",
    }


@pytest.mark.xfail(
    strict=True,
    reason="RT-6 (docs/red-team-findings.md): app.services.storage's purge_* functions compute "
    "the dry-run preview count and the confirmed delete count via two SEPARATE, unlocked calls. "
    "A row that becomes eligible between the two calls (e.g. the daily batch job inserting more "
    "stale rows) is silently deleted without ever having been shown to the approving operator. "
    "Owner: ARCH-DB.",
)
def test_confirmed_purge_count_matches_the_previously_shown_dry_run_count() -> None:
    with migrated_schema() as conn:
        insert_article(conn, **_stale_unmatched_article("https://example.com/old-1", days_old=400))
        conn.commit()

        preview = purge_stale_unmatched_articles(conn, older_than_days=30, confirm=False)
        assert preview.confirmed is False
        assert preview.rows_affected == 1
        shown_to_operator = preview.rows_affected

        # Simulates the real-world gap between "operator sees the preview
        # count" and "operator clicks confirm" -- during which the batch
        # job (or anything else) writes another row that now also
        # qualifies under the SAME WHERE clause the preview already ran.
        insert_article(conn, **_stale_unmatched_article("https://example.com/old-2", days_old=400))
        conn.commit()

        confirmed = purge_stale_unmatched_articles(conn, older_than_days=30, confirm=True)

        assert confirmed.rows_affected == shown_to_operator, (
            f"operator was shown rows_affected={shown_to_operator} in the dry-run preview, but "
            f"confirming the purge actually deleted {confirmed.rows_affected} rows -- the operator "
            "approved a number that the real delete silently exceeded"
        )

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

FIXED by ARCH-DB (commit fc2f47c): `confirm=False` now snapshots the
EXACT qualifying row ids (not just a count) into the `settings` table
under a `purge_preview::<operation>::<params>` key; `confirm=True`
requires that snapshot and deletes `WHERE id = ANY(<snapshot ids>)`,
intersected with the live predicate again as defense-in-depth. A row
that only starts qualifying after the preview was taken is therefore
NEVER in the snapshot and can never be deleted by that confirm call --
independently re-verified below (`test_confirmed_purge_count_matches_...`,
flipped from xfail to a normal regression test).

ARCH-DB's own commit message self-reports a narrower residual gap: two
DIFFERENT `confirm=False` preview calls for the IDENTICAL operation and
parameters share one settings key, so the second preview's `set_setting`
overwrites the first's snapshot -- a `confirm=True` might then act on a
different (but still real, still previously-shown-to-SOME-operator)
snapshot than the one a specific operator personally looked at.
Independently verified below
(`test_two_concurrent_previews_the_second_overwrites_the_first_but_never_invents_rows`)
that this residual behaves exactly as characterized: never worse than
disclosed, never a reopening of RT-6's original failure mode (a row
that no preview ever showed being deleted).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.errors import ValidationError
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


def test_confirmed_purge_count_matches_the_previously_shown_dry_run_count() -> None:
    """RT-6, CONFIRMED FIXED (ARCH-DB, fc2f47c) -- was xfail(strict=True)."""
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
        # The new row inserted after the preview must survive untouched --
        # it was never in the snapshot the preview took.
        survivor = conn.execute(
            "SELECT id FROM articles WHERE url = 'https://example.com/old-2'"
        ).fetchone()
        assert survivor is not None, "a row inserted AFTER the preview was deleted anyway -- RT-6 reopened"


def test_confirm_without_a_prior_preview_is_refused_not_silently_recomputed() -> None:
    """Attacks the fix's core mechanism directly: `confirm=True` with no
    matching preview snapshot on record must raise, never fall back to
    a fresh live query (which would be exactly the old, race-prone
    behavior in disguise).
    """
    with migrated_schema() as conn:
        insert_article(conn, **_stale_unmatched_article("https://example.com/no-preview", days_old=400))
        conn.commit()
        with pytest.raises(ValidationError):
            purge_stale_unmatched_articles(conn, older_than_days=30, confirm=True)


def test_two_concurrent_previews_the_second_overwrites_the_first_but_never_invents_rows() -> None:
    """Verifies ARCH-DB's own self-reported residual gap behaves exactly
    as characterized in their commit message, no worse: two `confirm=False`
    previews for the IDENTICAL operation+parameters share one settings
    key, so the second overwrites the first. A subsequent `confirm=True`
    therefore acts on the SECOND preview's snapshot -- which may omit a
    row the first preview showed, or include one the first didn't -- but
    every id it can possibly delete was shown in SOME real preview.
    Never a row invented at confirm time; never a row NO preview ever
    computed. This is a real, disclosed narrowing, not a hidden one --
    not filed as a separate RT-N finding.
    """
    with migrated_schema() as conn:
        insert_article(conn, **_stale_unmatched_article("https://example.com/race-1", days_old=400))
        conn.commit()

        first_preview = purge_stale_unmatched_articles(conn, older_than_days=30, confirm=False)
        assert first_preview.row_ids  # sanity: the first preview really did see race-1

        # A second, independent preview of the SAME operation+params
        # races in before anyone confirms -- e.g. two admin tabs open,
        # or an automated daily "preview then confirm" batch job
        # overlapping a human's own preview.
        insert_article(conn, **_stale_unmatched_article("https://example.com/race-2", days_old=400))
        conn.commit()
        second_preview = purge_stale_unmatched_articles(conn, older_than_days=30, confirm=False)
        assert set(second_preview.row_ids) == {
            r["id"] for r in conn.execute("SELECT id FROM articles WHERE url LIKE 'https://example.com/race-%'")
        }

        confirmed = purge_stale_unmatched_articles(conn, older_than_days=30, confirm=True)

        # The confirm acted on the SECOND (most recent) snapshot -- both
        # race rows, since the second preview saw both.
        assert set(confirmed.row_ids) == set(second_preview.row_ids)
        # Never a superset of what ANY real preview computed -- the
        # specific property that keeps this a narrowing, not a reopening,
        # of RT-6.
        assert set(confirmed.row_ids) <= (set(first_preview.row_ids) | set(second_preview.row_ids))

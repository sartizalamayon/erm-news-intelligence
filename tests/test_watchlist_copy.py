"""The prose `app/db/watchlist.py` puts on a Chief Risk Officer's screen.

`WatchGroup.blurb` and `WatchEntity.note` are not code comments. They
are rendered verbatim on the Long-term dashboard's Telecom watch tab
(`frontend/js/screens/dashboard_long.js::boardCard`, `boardRow`), where
they sit directly above and beside numbers that were genuinely measured
from the archive. Anything written in that voice inherits the
credibility of the numbers next to it, whether or not it earned it.

Two specific ways that went wrong, both of which shipped:

  * The operator board's blurb read "The four licensed mobile operators
    and the satellite entrant" -- five -- above a board that has always
    drawn SIX rows. The sixth, Airtel / Cirkle, is Robi's second brand
    and is not a separately licensed operator, so it was never covered
    by that sentence. A caption that miscounts the list underneath it is
    the one kind of error a board reader is guaranteed to notice,
    because counting the rows is what they came to do.
  * That same row's note said the brand was "renamed Cirkle in 2026", a
    dated claim about the outside world asserted in the present tense by
    a module with no way to check it, and no way to notice when it stops
    being true. This file can verify that headlines use both names; it
    cannot verify a corporate timeline.

Both checks below are general rules rather than assertions about the
exact strings, so they keep holding as entities are added.
"""

from __future__ import annotations

import re

from app.db.watchlist import WATCH_GROUPS

# Only as far as a board could plausibly grow. A blurb saying
# "thirty-seven" is not the failure mode this guards against.
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


def _counts_claimed(text: str) -> list[int]:
    """Every number a reader could take as "how many rows are below"."""
    found = [int(d) for d in re.findall(r"\b\d+\b", text)]
    for word, value in _NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", text, flags=re.IGNORECASE):
            found.append(value)
    return found


def test_a_group_blurb_never_claims_a_row_count_the_group_does_not_have() -> None:
    """A count in a caption is read as the count of the list below it.

    `all_entities()` is the single source of truth for how many rows a
    board draws. A number written into the blurb beside it is a second
    source of truth that nothing keeps in step, and the two had already
    drifted apart by one before this test existed.

    A blurb may still state a count -- the MFS board's "The two
    mobile-financial-services players" is true and useful -- it just has
    to be the real one.
    """
    offenders = []
    for group in WATCH_GROUPS:
        claimed = _counts_claimed(group.blurb)
        if claimed and len(group.entities) not in claimed:
            offenders.append(
                f"{group.key}: blurb says {claimed}, board draws {len(group.entities)} rows -- {group.blurb!r}"
            )
    assert offenders == [], "\n  ".join(
        ["a blurb states a count that is not the number of rows on that board:", *offenders]
    )


def test_no_rendered_watchlist_copy_dates_an_outside_world_event() -> None:
    """No years in blurbs or notes.

    A year in this file's rendered copy is always a claim about
    something that happened outside the database -- a rebrand, a licence
    award, a policy change -- stated as settled fact to a risk officer
    by code that cannot check it and will not notice when it is
    superseded. The airtel row carried exactly one of these ("renamed
    Cirkle in 2026") and it would have gone on being printed with total
    confidence through any number of later developments.

    What this module CAN stand behind is what its patterns match, so
    that is what the copy is allowed to describe. If a date genuinely
    has to appear on the dashboard, it belongs to a measured column with
    a window chip, not to a hand-written caption.
    """
    offenders = []
    for group in WATCH_GROUPS:
        for label, text in [(f"{group.key} blurb", group.blurb)] + [
            (f"{group.key}/{entity.key} note", entity.note) for entity in group.entities
        ]:
            for year in re.findall(r"\b(?:19|20)\d{2}\b", text):
                offenders.append(f"{label}: {year} in {text!r}")
    assert offenders == [], "\n  ".join(
        ["rendered watchlist copy must not date an outside-world event:", *offenders]
    )

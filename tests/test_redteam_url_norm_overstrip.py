"""RED-TEAM: duplicate-article dedup via `url_norm` (mission attack
surface "Duplicate articles ... URLs differing only by tracking params,
and whether url_norm normalization is consistent").

app.sources._util.normalize_url() strips a documented list of tracking
query params (legacy-behavior.md §2.6 point 3, ported verbatim):
`utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `ref`, `ref_src`,
`igshid`, `spm`, `cmpid`.

FIXED by P1-INGEST (commit 000576b): `_TRACKING_PARAM_PREFIXES` is now
`("utm_",)` only, with every other name matched exactly via a separate
`_TRACKING_PARAM_EXACT` frozenset. Re-verified directly against
`normalize_url()` (both by manual reproduction and by every case below,
run with `--runxfail` to see the real assertion outcome, not just the
strict-xfail summary label) for all 7 originally-flagged cases,
INCLUDING `spmid`/`cmpidx` specifically -- a 2026-08-07 coordinator
message flagged those two as "still failing", which traced back to
pytest's own strict-xfail-turned-XPASS report using the word "FAILED"
in its summary line for what is actually a pass (the fix working), NOT
a real assertion failure, compounded by this file's OWN cosmetic `ids=`
lambda mislabeling numeric-looking values (`spmid=9` was rendered as
`spmid-value`, `fbclid2=1` as `fbclid2-value`) in the displayed test ID.
Confirmed via a direct, unambiguous reproduction
(`normalize_url(...spmid=9) != normalize_url(...)` and same for
`cmpidx=q`) that both survive correctly -- RT-3 is fully closed, not
partially. The `ids=` bug (own file, fixed below) never affected which
inputs were tested, only how they were labeled in `-v` output.

Originally (see docs/red-team-findings.md, RT-3's write-up): the filter
applied `.startswith(bad)` to EVERY entry in a list, not just `utm_`,
so `referral_code`, `refresh_token`, `refcode`, `gclidextra`, `fbclid2`,
`spmid`, `cmpidx` (none of which are tracking params) were silently
stripped right along with the real ones -- collapsing two articles with
different, meaningful, non-tracking query parameters to the same
`url_norm`. Since app.services.collector.write_items()'s dedup-on-insert
treats an existing `url_norm` as "already have this article" (ON
CONFLICT DO NOTHING plus the seen_urls pre-check), this was not
cosmetic: a second, genuinely distinct article would be silently dropped
as a false-positive duplicate.
"""

from __future__ import annotations

import pytest

from app.sources._util import normalize_url


@pytest.mark.parametrize(
    ("distinguishing_param", "example_value"),
    [
        ("referral_code", "abc123"),
        ("refresh_token", "xyz"),
        ("refcode", "zz"),
        ("gclidextra", "zz"),
        ("fbclid2", "1"),
        ("spmid", "9"),
        ("cmpidx", "q"),
    ],
    # Explicit string ids -- the previous ids=lambda (v.isdigit() check)
    # mislabeled numeric-looking values as the literal "value" in -v
    # output, which is what made a real XPASS look ambiguous from the
    # outside (see module docstring). No cleverness needed here.
    ids=[
        "referral_code",
        "refresh_token",
        "refcode",
        "gclidextra",
        "fbclid2",
        "spmid",
        "cmpidx",
    ],
)
def test_non_tracking_param_survives_normalization(distinguishing_param: str, example_value: str) -> None:
    """RT-3, CONFIRMED FIXED (P1-INGEST, 000576b) -- was xfail(strict=True),
    now a normal regression test. Every one of these 7 params (including
    spmid/cmpidx, re-verified per the module docstring) must survive
    normalize_url() untouched.
    """
    with_param = f"https://example.com/news/story?title=x&{distinguishing_param}={example_value}"
    without_param = "https://example.com/news/story?title=x"

    normalized_with = normalize_url(with_param)
    normalized_without = normalize_url(without_param)

    assert normalized_with != normalized_without, (
        f"a genuinely distinct query param ({distinguishing_param}={example_value}) must not be "
        f"stripped -- both URLs normalized to {normalized_with!r}, which will make "
        "app.services.collector.write_items() treat these as the SAME article and silently drop one"
    )


def test_documented_exact_tracking_params_are_still_correctly_stripped() -> None:
    """Sanity check, NOT xfail: the real tracking params this filter is
    meant to catch must still be caught after any fix -- this pins the
    intended behavior so a fix for RT-3 can't overcorrect into leaving
    genuine tracking params in url_norm.
    """
    base = "https://example.com/news/story?title=x"
    tracked_variants = [
        "https://example.com/news/story?title=x&utm_source=facebook",
        "https://example.com/news/story?title=x&fbclid=IwAR123",
        "https://example.com/news/story?title=x&gclid=abc",
        "https://example.com/news/story?title=x&ref=share",
        "https://example.com/news/story?title=x&ref_src=twsrc",
        "https://example.com/news/story?title=x&spm=1.2",
        "https://example.com/news/story?title=x&cmpid=email",
        "https://example.com/news/story?title=x&mc_cid=abc&mc_eid=def",
        "https://example.com/news/story?title=x&igshid=xyz",
    ]
    expected = normalize_url(base)
    for variant in tracked_variants:
        assert normalize_url(variant) == expected, f"{variant!r} should normalize to {expected!r}"


def test_normalize_url_is_idempotent_after_the_rt3_fix() -> None:
    """Extra coverage from the coordinator's second-pass attack angle
    ("does normalization stay idempotent and stable"): applying
    normalize_url() twice must be a no-op the second time, for both
    over-strip survivors and genuine tracking params, mixed case, a
    duplicated key, and a Google News redirect unwrap. Passed on first
    write -- not a fix verification, just closing a gap in RT-3's
    original coverage.
    """
    candidates = [
        "https://example.com/a?utm_source=x&utm_content_id=1",
        "https://example.com/a?REF=x&Title=y",
        "https://example.com/a?ref=1&ref=2",
        "https://example.com/a?utm_source=x&utm_source=y",
        "https://news.google.com/rss/articles/xyz?url=https://example.com/real-article%3Fref=abc",
        "https://example.com/a?spmid=9&cmpidx=q&referral_code=abc123",
    ]
    for url in candidates:
        once = normalize_url(url)
        twice = normalize_url(once)
        assert once == twice, f"normalize_url is not idempotent for {url!r}: {once!r} -> {twice!r}"

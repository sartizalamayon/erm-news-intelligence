"""RED-TEAM: duplicate-article dedup via `url_norm` (mission attack
surface "Duplicate articles ... URLs differing only by tracking params,
and whether url_norm normalization is consistent").

app.sources._util.normalize_url() strips a documented list of tracking
query params (legacy-behavior.md §2.6 point 3, ported verbatim):
`utm_*`, `fbclid`, `gclid`, `mc_cid`, `mc_eid`, `ref`, `ref_src`,
`igshid`, `spm`, `cmpid`.

Finding (see docs/red-team-findings.md, RT-3): the filter is implemented
as

    if not any(k.lower().startswith(bad) or k.lower() == bad for bad in _TRACKING_PARAM_PREFIXES)

applying `.startswith(bad)` to EVERY entry in the list, not just the one
(`utm_`) that is genuinely meant to be a prefix. `ref`, `fbclid`,
`gclid`, `spm`, `cmpid`, `mc_cid`, `mc_eid`, `ref_src`, and `igshid` are
all meant to be exact query-param NAMES, but `.startswith()` turns each
of them into a prefix match too. Any query parameter whose name merely
*starts with* one of those strings -- `referral_code`, `refresh_token`,
`gclidextra`, `spmid`, `fbclid2`, none of which are tracking params --
gets silently stripped right along with the real ones.

Consequence: two articles at the same path with DIFFERENT, meaningful,
non-tracking query parameters that happen to share one of these
prefixes collapse to the identical `url_norm`. Since
app.services.collector.write_items()'s dedup-on-insert treats an
existing `url_norm` as "already have this article" and skips the insert
entirely (app/db/queries.py's `ON CONFLICT (url_norm) ... DO NOTHING`,
plus the `seen_urls` pre-check), this is not cosmetic: a second,
genuinely distinct article is silently dropped as a false-positive
duplicate, with no error, no log distinguishable from a real duplicate,
and no way for a human reviewer to notice the row never arrived.
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
    ids=lambda v: v if isinstance(v, str) and not v.isdigit() else "value",
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-3 (docs/red-team-findings.md): normalize_url()'s tracking-param filter uses "
    "str.startswith() against every entry in _TRACKING_PARAM_PREFIXES, not just the genuine "
    "prefix ('utm_'). Non-tracking params merely starting with 'ref', 'gclid', 'fbclid', 'spm', "
    "or 'cmpid' get stripped too, collapsing distinct URLs to the same url_norm and causing a "
    "real, distinct article to be silently dropped as a false-positive duplicate. Owner: P1-INGEST.",
)
def test_non_tracking_param_survives_normalization(distinguishing_param: str, example_value: str) -> None:
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

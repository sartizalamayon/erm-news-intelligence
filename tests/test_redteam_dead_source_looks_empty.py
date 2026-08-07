"""RED-TEAM: "Empty feeds" attack surface — a source returning zero items.
Mission brief, verbatim: "Does the run continue? Is it distinguishable
from 'source succeeded, genuinely no news today'?"

app/sources/_sitemap.py ships TWO entry points on purpose:

  * `fetch_news_sitemap()` — returns `[]` whenever nothing pans out,
    whether that's "sitemap fetched fine, zero fresh articles today" OR
    "every candidate URL came back unreachable". No distinction.
  * `require_news_sitemap()` — same fallback chain, but does one extra
    reachability probe and raises `SourceFetchError` when the primary
    URL is truly dead, specifically so a totally-down source is reported
    as `status="failed"`, not `status="empty"`. Its own docstring says
    so explicitly: "for sources whose ABC method should treat total
    unreachability as a per-source failure rather than a silent empty
    result."

Finding (see docs/red-team-findings.md, RT-2): grepping every ported
source file, `require_news_sitemap` has exactly ONE caller in the whole
codebase — nothing under app/sources/*.py. 9 sources in the
highest-volume "sitemap" method family (legacy-behavior.md §2.2's core
6 -- Daily Inqilab, Dhaka Post, Desh Rupantor, The Business Standard,
Dhaka Mail, CNN -- plus 3 more added in P1-INGEST's later follow-up pass
that widened the registry to 45/51 sources -- Ajker Patrika, BD News 24,
Channels) call the bare `fetch_news_sitemap()` instead. Two of the three
follow-up sources (Ajker Patrika, BD News 24) additionally fall back to
`fetch_rss(..., required=False)` when the sitemap is empty -- which
ALSO returns `[]` rather than raising on total unreachability -- so the
same symptom survives even through that fallback hop. For all 9 of the
currently-ported sources, a fully dead host (DNS failure, TLS failure,
connection refused, every candidate 404/500) is byte-for-byte
indistinguishable from "the source is healthy and simply published
nothing new today" — both come back as `SourceResult(status="empty")`
from app.services.collector._run_one_source, never `status="failed"`.
This directly contradicts the safety mechanism the same module already
built and documents, one call away, unused.

Uses the exact FakeSession/FakeResponse doubles app/sources' own test
suite uses (tests/test_p1_helpers.py, P1-FETCH's file — imported
read-only here, never modified) so this reproduces with zero real
network, exactly mirroring
tests/test_p1_sitemap_family.py::TestFetchNewsSitemap::test_empty_when_no_candidate_works,
which already proves `fetch_news_sitemap` returns `[]` on total
unreachability -- this file's job is to show that result then reaches
the real, registered `CnnSource`/`DhakaPostSource`/etc. and comes out
the collector's other end as "empty", not "failed".

FIXED by P1-INGEST (commit 000576b): all 9 sources now call
`require_news_sitemap()` (directly, or via a two-stage
try/except-then-RSS-fallback for the two sources that have one), and
`require_news_sitemap()` itself was widened to treat a definitive
non-200 on its reachability probe as unreachable too (the original only
handled "no response at all"), which matters because this file's own
reproduction uses all-404s, not a raw connection error. All 9
parametrized cases below flipped from xfail(strict=True) to a normal
regression test, plus one ADDED case (this file's own second-pass
attack) proving the fix also holds for the OTHER failure shape --
a raw connection error / DNS failure (no response at all), not just a
definitive 404 -- since those exercise genuinely different code paths
inside app.sources.http.get() (the retry-then-give-up branch vs. the
got-a-response-but-it-was-4xx branch).
"""

from __future__ import annotations

import pytest
import requests

import app.sources.ajker_patrika as ajker_patrika_module
import app.sources.bd_news_24 as bd_news_24_module
import app.sources.business_standard as business_standard_module
import app.sources.channels as channels_module
import app.sources.cnn as cnn_module
import app.sources.daily_inqilab as daily_inqilab_module
import app.sources.desh_rupantor as desh_rupantor_module
import app.sources.dhaka_mail as dhaka_mail_module
import app.sources.dhaka_post as dhaka_post_module
from app.services.collector import _run_one_source
from app.sources import _sitemap
from tests.test_p1_helpers import FakeSession

# One (module, class) pair per source in the "sitemap" method family
# (legacy-behavior.md §2.2's original 6-source table, plus 3 more added
# to the registry in P1-INGEST's later follow-up pass) -- every single
# one of them wires iter_feed_items() straight to
# app.sources._sitemap.fetch_news_sitemap, with or without a same-symptom
# RSS fallback (Ajker Patrika, BD News 24 -- see module docstring).
_SITEMAP_FAMILY_SOURCES = (
    (daily_inqilab_module, daily_inqilab_module.DailyInqilabSource),
    (dhaka_post_module, dhaka_post_module.DhakaPostSource),
    (desh_rupantor_module, desh_rupantor_module.DeshRupantorSource),
    (business_standard_module, business_standard_module.BusinessStandardSource),
    (dhaka_mail_module, dhaka_mail_module.DhakaMailSource),
    (cnn_module, cnn_module.CnnSource),
    (ajker_patrika_module, ajker_patrika_module.AjkerPatrikaSource),
    (bd_news_24_module, bd_news_24_module.BdNews24Source),
    (channels_module, channels_module.ChannelsSource),
)


def _totally_dead_session() -> FakeSession:
    """A FakeSession that 404s absolutely everything -- the primary
    sitemap URL, robots.txt, and every NEWS_SITEMAP_PATHS fallback
    candidate. Mirrors a genuinely dead host: DNS resolves nowhere new
    is required, every request the module tries just fails to find
    anything. requests.Session.request() would behave the same for a
    TLS handshake failure or connection-refused too, since
    app.sources.http.get() only ever returns None (after retries) or a
    real Response -- FakeSession here stands in at that same seam.
    """
    return FakeSession()  # any unregistered URL -> 404, per its own docstring


class _AlwaysConnectionErrorSession:
    """The OTHER unreachability shape: no response at all (DNS failure /
    connection refused / TLS handshake failure), as opposed to
    `_totally_dead_session`'s "server answers, every URL 404s". These
    exercise genuinely different branches inside app.sources.http.get()
    (the retry-then-exhausted branch returning None from a caught
    `requests.exceptions.RequestException`, vs. the
    got-a-response-but-non-200 branch) -- worth checking separately
    since RT-2's fix specifically had to widen `require_news_sitemap`'s
    probe to handle BOTH (see that function's current docstring: the
    original only handled "no response at all").
    """

    def request(self, method, url, **kwargs):
        raise requests.exceptions.ConnectionError("simulated: host unreachable")


@pytest.mark.parametrize("module_and_cls", _SITEMAP_FAMILY_SOURCES, ids=lambda pair: pair[1].key)
def test_totally_dead_sitemap_source_is_reported_failed_not_empty(
    module_and_cls: tuple[object, type], monkeypatch: pytest.MonkeyPatch
) -> None:
    """RT-2, CONFIRMED FIXED (P1-INGEST, 000576b) -- was xfail(strict=True)."""
    module, source_cls = module_and_cls
    # Every source's __init__ does `self.session = make_session(pool_size=...)`
    # via `from app.sources.http import make_session` -- patch the NAME as
    # bound in the source's own module (not app.sources.http itself), which
    # is what `source_cls()` actually calls.
    monkeypatch.setattr(module, "make_session", lambda *a, **k: _totally_dead_session())

    result, items = _run_one_source(source_cls.key, source_cls)

    assert items == []  # not in dispute -- both readings agree nothing was fetched
    assert result.status == "failed", (
        f"{source_cls.__name__}: a totally unreachable host must not be reported the same way as "
        f"'source is healthy, zero new articles today' -- got status={result.status!r}"
    )


@pytest.mark.parametrize(
    "module_and_cls",
    [
        (daily_inqilab_module, daily_inqilab_module.DailyInqilabSource),  # pure require_news_sitemap
        (ajker_patrika_module, ajker_patrika_module.AjkerPatrikaSource),  # two-stage + RSS fallback
    ],
    ids=lambda pair: pair[1].key,
)
def test_connection_error_unreachability_also_reported_failed_not_empty(
    module_and_cls: tuple[object, type], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second-pass attack angle beyond RT-2's original reproduction (all
    404s): a raw connection failure (DNS/refused/TLS) -- no response at
    all -- must ALSO be reported status='failed', for both the plain
    require_news_sitemap sources and the two-stage RSS-fallback ones.
    """
    from app.sources import http as http_module

    module, source_cls = module_and_cls
    monkeypatch.setattr(module, "make_session", lambda *a, **k: _AlwaysConnectionErrorSession())
    # Every failed attempt sleeps for real (jittered backoff) unless
    # patched out -- this test deliberately exhausts every retry across
    # several call sites (primary + robots.txt + candidates), so without
    # this it burns real wall-clock time for no additional coverage.
    monkeypatch.setattr(http_module.time, "sleep", lambda *_: None)

    result, items = _run_one_source(source_cls.key, source_cls)

    assert items == []
    assert result.status == "failed", (
        f"{source_cls.__name__}: a connection-refused/DNS-failure host must be reported "
        f"status='failed', not {result.status!r}"
    )


def test_require_news_sitemap_is_now_used_by_every_sitemap_family_source() -> None:
    """Was `test_require_news_sitemap_exists_and_is_unused_by_any_ported_source`
    (documenting the gap); now documents the fix the same way -- every
    one of the 9 sources this file tracks calls `require_news_sitemap`
    somewhere in its `iter_feed_items()`, not the bare `fetch_news_sitemap`
    alone.
    """
    assert callable(_sitemap.require_news_sitemap)
    import inspect

    for module, _cls in _SITEMAP_FAMILY_SOURCES:
        source_code = inspect.getsource(module)
        assert "require_news_sitemap" in source_code, f"{module.__name__} no longer calls require_news_sitemap"

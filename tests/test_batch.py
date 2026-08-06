"""app.batch: the daily entrypoint runs standalone (no import of app.main
or anything under app.api) and exits 0 on an empty source registry —
proves DoD #1's "importable and runnable independently of the web app".

Updated by P1-FETCH: the registry is no longer naturally empty (21 real
sources are registered — app/sources/registry.py), so "empty registry"
is now an explicit test setup (monkeypatched), not the default state.
Real source classes are never exercised here — several of them (the
curl-based Kalbela/Daily Sun) shell out to the system `curl` binary,
which runs as a SEPARATE OS process and is NOT intercepted by
tests/conftest.py's socket-level network guard; letting a test reach
those sources for real would silently defeat "no network access in
tests" rather than violate it loudly. app/services/collector.py's
per-source isolation (fake sources, no real network) is covered
separately in tests/test_p1_collector.py.
"""

from __future__ import annotations

from app.batch.run_daily import main


def test_main_returns_zero_with_no_registered_sources(monkeypatch):
    monkeypatch.setattr("app.batch.run_daily.all_sources", lambda: {})
    assert main() == 0

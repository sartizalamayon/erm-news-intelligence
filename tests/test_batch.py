"""app.batch: the daily entrypoint runs standalone (no import of app.main
or anything under app.api) and exits 0 on an empty source registry —
proves DoD #1's "importable and runnable independently of the web app"
before any real fetch logic exists.
"""

from __future__ import annotations

from app.batch.run_daily import main


def test_main_returns_zero_with_no_registered_sources():
    assert main() == 0

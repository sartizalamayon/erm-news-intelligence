"""CLI entrypoint: `python -m app.batch`.

This is what GitHub Actions cron (once a daily-batch workflow exists)
invokes. Kept to a two-line dispatch on purpose — the real orchestration
lives in run_daily.main() so it stays independently importable/testable
without going through `__main__`.
"""

from app.batch.run_daily import main

if __name__ == "__main__":
    raise SystemExit(main())

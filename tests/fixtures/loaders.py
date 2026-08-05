"""Load the committed, frozen fixture tiers under `fixtures/` at the repo
root. Tests import from here rather than opening JSON files directly, so
there is exactly one place that knows the current freeze date and file
layout — bump `FREEZE_DATE` (and re-run `tests/fixtures/build_fixtures.py`)
the day these are regenerated, nowhere else.

Deliberately does NOT touch `data/news.db` — these loaders only ever read
the committed JSON, which is why they work identically in CI (no local DB)
and on a developer machine.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES_DIR = REPO_ROOT / "fixtures"

# The freeze/generation date suffix on the current fixture files (see
# tests/fixtures/build_fixtures.py's module docstring for why this is the
# script's RUN date, not the underlying articles' collection date).
FREEZE_DATE = "20260806"


def _load(name: str) -> dict[str, Any]:
    path = FIXTURES_DIR / name
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Fixtures are committed to git (see "
            "docs/test-strategy.md 'Fixture system'); if this is failing in CI, "
            "the checkout is missing files, not missing data/news.db access — "
            "these loaders never touch the live DB."
        )
    return json.loads(path.read_text(encoding="utf-8"))


@cache
def load_keywords_snapshot() -> dict[str, Any]:
    return _load(f"keywords_snapshot_{FREEZE_DATE}.json")


@cache
def load_p1_raw() -> dict[str, Any]:
    return _load(f"p1_raw_{FREEZE_DATE}.json")


@cache
def load_p2_matched() -> dict[str, Any]:
    return _load(f"p2_matched_{FREEZE_DATE}.json")


@cache
def load_p3_golden() -> dict[str, Any]:
    return _load(f"p3_golden_{FREEZE_DATE}.json")

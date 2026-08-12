"""Live macro-indicator fetches for the dashboard's long-term "trend
cards" (frontend-contract.md §3.2 — Taka/USD, Brent crude, Bangladesh
CPI inflation). Consumed by `app.api.routers.dashboard`'s
`GET /dashboard/trends`; remittance has no verified free API
(Bangladesh Bank exposes none) so it is deliberately NOT fetched here —
that router instead reads/writes an admin-entered manual value via
`app.db.settings.get_setting`/`set_setting`, the same KV pattern
`relevance_guidance` uses (app.services.llm.prompts).

Every `fetch_*` function below:
  * NEVER raises into its caller. Any network error, timeout, non-2xx
    status, or unexpected response shape is caught, logged at WARNING,
    and turned into `None` — an external API being down must not break
    the dashboard request path (this module's explicit mandate).
  * Is cached in-process (a plain module-level dict; no DB table, no new
    dependency) for a TTL matched to how often the source actually
    changes, so a page full of dashboard viewers never re-hits these
    APIs per-request. This matters most for oilpriceapi.com's free tier
    (200 requests/month, ~6/day) — caching keeps this module's own
    traffic to it a small fraction of that even under regular use.

Sources (all free; the first two need no signup at all):
  * USD/BDT   — https://open.er-api.com/v6/latest/USD ("open.er-api.com",
                keyless, `rates.BDT`). Verified live 2026-08-12: 123.526916.
  * Inflation — https://api.worldbank.org/v2/country/BD/indicator/FP.CPI.TOTL.ZG
                (World Bank Open Data, keyless). Annual data only, ~1yr
                publication lag — there is no free source with finer
                (e.g. monthly) granularity for Bangladesh CPI; the most
                recent non-null year in the response is used as-is.
  * Brent     — https://api.oilpriceapi.com/v1/prices/latest?by_code=BRENT_CRUDE_USD
                (oilpriceapi.com). Free tier, 200 req/month, no credit
                card, but DOES require a free account —
                https://www.oilpriceapi.com/auth/signup — for an API
                key. `fetch_brent()` takes that key as a parameter
                (read by the caller from `Settings.oilpriceapi_key`) and
                returns `None` immediately, with no HTTP call at all,
                when it's blank — an unconfigured deployment must never
                touch this free tier's quota.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from app.logging import get_logger

logger = get_logger(__name__)

REQUEST_TIMEOUT_SECONDS = 10

USD_BDT_URL = "https://open.er-api.com/v6/latest/USD"
WORLDBANK_INFLATION_URL = (
    "https://api.worldbank.org/v2/country/BD/indicator/FP.CPI.TOTL.ZG?format=json&per_page=5"
)
OILPRICEAPI_URL = "https://api.oilpriceapi.com/v1/prices/latest?by_code=BRENT_CRUDE_USD"

# Cache TTLs — deliberately generous. None of these three sources update
# faster than daily; the exchange rate and Brent are refreshed a few
# times a day at most, inflation is annual data.
USD_BDT_TTL_SECONDS = 6 * 3600  # 6h
BRENT_TTL_SECONDS = 6 * 3600  # 6h
INFLATION_TTL_SECONDS = 24 * 3600  # 1 day


@dataclass(frozen=True)
class MacroValue:
    """One fetched (or cache-hit) macro reading."""

    value: float
    fetched_at: datetime


# key -> (value, monotonic-clock expiry). Process-wide and deliberately
# simple — no DB table, no extra dependency; a single dict is enough for
# 3 keys with hour-scale TTLs.
_cache: dict[str, tuple[MacroValue, float]] = {}


def _cached(key: str, ttl_seconds: float, do_fetch: Callable[[], MacroValue | None]) -> MacroValue | None:
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[1] > now:
        return hit[0]
    value = do_fetch()
    if value is not None:
        _cache[key] = (value, now + ttl_seconds)
    return value


def _clear_cache_for_tests() -> None:
    """Test-only escape hatch (tests/test_macro.py) — clears the
    module-level cache so one test's monkeypatched `requests.get` can't
    leak a cached value into the next test.
    """
    _cache.clear()


def fetch_usd_bdt() -> MacroValue | None:
    """USD -> BDT spot exchange rate, open.er-api.com (free, keyless).
    Cached ~6h. Returns None on any failure — never raises.
    """

    def _do_fetch() -> MacroValue | None:
        try:
            resp = requests.get(USD_BDT_URL, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            data = resp.json()
            rate = data["rates"]["BDT"]
            return MacroValue(value=float(rate), fetched_at=datetime.now(timezone.utc))
        except Exception:
            logger.warning("macro.fetch_usd_bdt: fetch failed, leaving indicator unchanged", exc_info=True)
            return None

    return _cached("usd_bdt", USD_BDT_TTL_SECONDS, _do_fetch)


def fetch_inflation() -> MacroValue | None:
    """Bangladesh year-over-year CPI inflation (%), World Bank
    FP.CPI.TOTL.ZG (free, keyless). Annual data, ~1yr publication lag —
    the most recent year in the response with a non-null value is used.
    Cached ~1 day. Returns None on any failure — never raises.
    """

    def _do_fetch() -> MacroValue | None:
        try:
            resp = requests.get(WORLDBANK_INFLATION_URL, timeout=REQUEST_TIMEOUT_SECONDS)
            resp.raise_for_status()
            payload = resp.json()
            # World Bank's shape: [ {page metadata}, [ {date, value}, ... newest first ] ]
            rows = payload[1] if isinstance(payload, list) and len(payload) > 1 and payload[1] else []
            for row in rows:
                if isinstance(row, dict) and row.get("value") is not None:
                    return MacroValue(value=float(row["value"]), fetched_at=datetime.now(timezone.utc))
            return None
        except Exception:
            logger.warning("macro.fetch_inflation: fetch failed, leaving indicator unchanged", exc_info=True)
            return None

    return _cached("inflation", INFLATION_TTL_SECONDS, _do_fetch)


def fetch_brent(api_key: str) -> MacroValue | None:
    """Brent crude spot price (USD/bbl), oilpriceapi.com. Requires a
    free API key (app.config.Settings.oilpriceapi_key, signup at
    https://www.oilpriceapi.com/auth/signup) — returns None immediately
    with NO HTTP call at all when `api_key` is blank, so an unconfigured
    deployment never touches the 200 req/month free tier. Cached ~6h.
    Returns None on any failure — never raises.
    """
    if not api_key:
        return None

    def _do_fetch() -> MacroValue | None:
        try:
            resp = requests.get(
                OILPRICEAPI_URL,
                headers={"Authorization": f"Token {api_key}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            payload = resp.json()
            price = payload["data"]["price"]
            return MacroValue(value=float(price), fetched_at=datetime.now(timezone.utc))
        except Exception:
            logger.warning("macro.fetch_brent: fetch failed, leaving indicator unchanged", exc_info=True)
            return None

    return _cached("brent", BRENT_TTL_SECONDS, _do_fetch)

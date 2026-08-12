"""Tests for app.services.macro -- the live macro-indicator fetches
behind the dashboard's long-term trend cards (Taka/USD, Brent crude,
Bangladesh CPI inflation). `requests.get` is monkeypatched directly in
every test here -- the network guard (tests/helpers/network_guard.py,
autouse) would block a real socket anyway, but these tests need
deterministic control over the response shape (status, JSON body,
timeouts) that a blocked socket alone can't simulate. Same convention
as tests/test_auth_email_providers.py.

Every `fetch_*` function must NEVER raise -- an external API being down
must not break the dashboard request path. That "never raise" contract
is exercised directly below (timeouts, non-2xx, malformed JSON, missing
keys all resolve to None, not an exception).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import Mock

import pytest

from app.services import macro as macro_module


class _FakeResponse:
    def __init__(self, status_code: int, json_body: Any = None):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json_body


@pytest.fixture(autouse=True)
def _clear_macro_cache():
    """The module-level cache is process-global -- clear it before AND
    after every test so a cached value from one test can never leak
    into the next (or beyond this file).
    """
    macro_module._clear_cache_for_tests()
    yield
    macro_module._clear_cache_for_tests()


# ── fetch_usd_bdt ────────────────────────────────────────────────────────


def test_fetch_usd_bdt_returns_real_value(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(200, {"result": "success", "rates": {"BDT": 123.526916}}))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    result = macro_module.fetch_usd_bdt()

    assert result is not None
    assert result.value == pytest.approx(123.526916)
    assert isinstance(result.fetched_at, datetime)
    fake_get.assert_called_once()
    assert fake_get.call_args.args[0] == macro_module.USD_BDT_URL


def test_fetch_usd_bdt_caches_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(200, {"rates": {"BDT": 123.5}}))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    first = macro_module.fetch_usd_bdt()
    second = macro_module.fetch_usd_bdt()

    assert first == second
    fake_get.assert_called_once()  # second call served from cache, no re-fetch


def test_fetch_usd_bdt_never_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(503, {}))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    assert macro_module.fetch_usd_bdt() is None


def test_fetch_usd_bdt_never_raises_on_network_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(macro_module.requests, "get", _boom)

    assert macro_module.fetch_usd_bdt() is None


def test_fetch_usd_bdt_never_raises_on_malformed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(200, {"unexpected": "shape"}))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    assert macro_module.fetch_usd_bdt() is None


# ── fetch_inflation ──────────────────────────────────────────────────────


def test_fetch_inflation_returns_most_recent_non_null_year(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {"page": 1, "pages": 1},
        [
            {"date": "2025", "value": 8.77},
            {"date": "2024", "value": 9.73},
        ],
    ]
    fake_get = Mock(return_value=_FakeResponse(200, payload))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    result = macro_module.fetch_inflation()

    assert result is not None
    assert result.value == pytest.approx(8.77)


def test_fetch_inflation_skips_null_years(monkeypatch: pytest.MonkeyPatch) -> None:
    """World Bank returns the most recent year first even when its value
    hasn't been published yet (null) -- the first NON-null year must be
    used instead of the newest row unconditionally.
    """
    payload = [
        {"page": 1},
        [
            {"date": "2026", "value": None},
            {"date": "2025", "value": 8.77},
        ],
    ]
    fake_get = Mock(return_value=_FakeResponse(200, payload))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    result = macro_module.fetch_inflation()

    assert result is not None
    assert result.value == pytest.approx(8.77)


def test_fetch_inflation_never_raises_when_all_years_null(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [{"page": 1}, [{"date": "2026", "value": None}]]
    fake_get = Mock(return_value=_FakeResponse(200, payload))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    assert macro_module.fetch_inflation() is None


def test_fetch_inflation_never_raises_on_network_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(macro_module.requests, "get", _boom)

    assert macro_module.fetch_inflation() is None


# ── fetch_brent ──────────────────────────────────────────────────────────


def test_fetch_brent_returns_none_and_makes_no_call_when_key_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(200, {"data": {"price": 82.4}}))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    result = macro_module.fetch_brent("")

    assert result is None
    fake_get.assert_not_called()  # unconfigured must never touch the free-tier quota


def test_fetch_brent_returns_real_value_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(
        return_value=_FakeResponse(
            200, {"status": "success", "data": {"price": 89.21, "code": "BRENT_CRUDE_USD"}}
        )
    )
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    result = macro_module.fetch_brent("test-api-key")

    assert result is not None
    assert result.value == pytest.approx(89.21)
    fake_get.assert_called_once()
    assert fake_get.call_args.args[0] == macro_module.OILPRICEAPI_URL
    assert fake_get.call_args.kwargs["headers"]["Authorization"] == "Token test-api-key"


def test_fetch_brent_never_raises_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(401, {"error": "invalid token"}))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    assert macro_module.fetch_brent("bad-key") is None


def test_fetch_brent_never_raises_on_network_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("simulated DNS failure")

    monkeypatch.setattr(macro_module.requests, "get", _boom)

    assert macro_module.fetch_brent("some-key") is None


def test_fetch_brent_caches_per_call_not_per_key_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """Documents the current cache behavior: the cache key is fixed
    ("brent"), so a config change to a different key within the TTL
    window still serves the previously cached value -- acceptable given
    keys don't rotate live and the TTL is a few hours, but worth pinning
    down explicitly so a future change to this is deliberate.
    """
    fake_get = Mock(return_value=_FakeResponse(200, {"data": {"price": 80.0}}))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    first = macro_module.fetch_brent("key-a")
    second = macro_module.fetch_brent("key-b")

    assert first == second
    fake_get.assert_called_once()


# ── MacroValue / fetched_at ──────────────────────────────────────────────


def test_fetched_at_is_timezone_aware_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(200, {"rates": {"BDT": 123.0}}))
    monkeypatch.setattr(macro_module.requests, "get", fake_get)

    result = macro_module.fetch_usd_bdt()

    assert result is not None
    assert result.fetched_at.tzinfo is not None
    assert result.fetched_at.utcoffset() == timezone.utc.utcoffset(None)

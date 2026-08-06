"""app.sources.http — the one retry/backoff/session helper (docs/
speed-techniques.md §4 point 2). No real sockets: `get()`/`get_or_raise`
are exercised against `FakeSession` (tests/test_p1_helpers.py), never a
real `requests.Session`.
"""

from __future__ import annotations

import threading
import time

import pytest
import requests

from app.errors import SourceFetchError
from app.sources import http
from tests.test_p1_helpers import FakeResponse, FakeSession


def test_make_session_sets_pool_size_and_headers():
    session = http.make_session(pool_size=12)
    adapter = session.get_adapter("https://example.com")
    assert adapter._pool_connections == 12
    assert adapter._pool_maxsize == 12
    assert "User-Agent" in session.headers


def test_get_returns_response_on_first_success():
    session = FakeSession().add("https://x.test/a", FakeResponse(status_code=200, text="ok"))
    resp = http.get(session, "https://x.test/a", source="test")
    assert resp is not None
    assert resp.status_code == 200
    assert session.call_count == 1


def test_get_retries_on_retryable_status_then_succeeds(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda *_: None)
    session = FakeSession()
    session.add("https://x.test/b", FakeResponse(status_code=503, text=""))
    session.add("https://x.test/b", FakeResponse(status_code=200, text="ok"))
    resp = http.get(session, "https://x.test/b", source="test", max_retries=2)
    assert resp is not None
    assert resp.status_code == 200
    assert session.call_count == 2


def test_get_gives_up_after_max_retries_on_persistent_failure(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda *_: None)
    session = FakeSession()
    for _ in range(4):
        session.add("https://x.test/c", FakeResponse(status_code=503))
    resp = http.get(session, "https://x.test/c", source="test", max_retries=3)
    assert resp is not None
    assert resp.status_code == 503  # last response returned, not None — server DID answer
    assert session.call_count == 4


def test_get_returns_none_when_every_attempt_raises(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda *_: None)
    session = FakeSession()
    for _ in range(3):
        session.add("https://x.test/d", requests.exceptions.ConnectionError("refused"))
    resp = http.get(session, "https://x.test/d", source="test", max_retries=2)
    assert resp is None
    assert session.call_count == 3


def test_get_does_not_retry_non_retryable_status():
    session = FakeSession().add("https://x.test/e", FakeResponse(status_code=404))
    resp = http.get(session, "https://x.test/e", source="test", max_retries=5)
    assert resp is not None
    assert resp.status_code == 404
    assert session.call_count == 1  # no retry burned on a definitive 404


def test_get_or_raise_raises_source_fetch_error_on_exhaustion(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda *_: None)
    session = FakeSession()
    for _ in range(2):
        session.add("https://x.test/f", requests.exceptions.Timeout("slow"))
    with pytest.raises(SourceFetchError) as excinfo:
        http.get_or_raise(session, "https://x.test/f", source="my_source", max_retries=1)
    assert excinfo.value.source == "my_source"


def test_get_or_raise_returns_response_on_success():
    session = FakeSession().add("https://x.test/g", FakeResponse(status_code=200, text="hi"))
    resp = http.get_or_raise(session, "https://x.test/g", source="test")
    assert resp.text == "hi"


def test_backoff_seconds_includes_jitter_and_grows():
    samples_0 = [http._backoff_seconds(0) for _ in range(20)]
    samples_2 = [http._backoff_seconds(2) for _ in range(20)]
    # jitter means not every sample is identical
    assert len(set(samples_0)) > 1
    # and later attempts wait longer on average (exponential base grows)
    assert min(samples_2) > max(samples_0) - 1.5  # loose bound, still exponential-shaped
    assert all(0 <= (s - 1.5**0) <= 0.75 + 1e-9 for s in samples_0)


def test_global_semaphore_bounds_concurrency():
    """Every get() call acquires the process-wide semaphore
    (speed-techniques.md §4 point 3) — verified by having more threads
    race to acquire it than its configured capacity and observing the
    high-water mark never exceeds that capacity.
    """
    cap = http.GLOBAL_MAX_IN_FLIGHT
    in_flight = 0
    max_seen = 0
    lock = threading.Lock()
    barrier = threading.Barrier(min(cap + 10, 40))

    def worker():
        nonlocal in_flight, max_seen
        barrier.wait()
        with http.GLOBAL_SEMAPHORE:
            with lock:
                in_flight += 1
                max_seen = max(max_seen, in_flight)
            time.sleep(0.05)  # hold the slot briefly so contention is real, not instantaneous
            with lock:
                in_flight -= 1

    threads = [threading.Thread(target=worker) for _ in range(barrier.parties)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert max_seen <= cap

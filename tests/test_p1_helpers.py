"""Shared synthetic-HTTP test doubles for the tests/test_p1_*.py suite.

No real sockets anywhere in this file — `FakeSession` stands in for
`requests.Session` at the exact interface `app.sources.http.get()` calls
(`.request(method, url, timeout=..., headers=..., ...)`), so every
method-family test can exercise real parsing/pagination/retry logic
against scripted responses without touching the network (the autouse
guard in tests/conftest.py would block a real socket anyway — this file
exists so tests never even try).

Matches the "tests/test_p1_*.py" file-ownership glob (P1-FETCH's own
files), so this lives here rather than under tests/helpers/** (ARCH-TEST's
territory, off-limits per the mission's file list).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeResponse:
    status_code: int = 200
    text: str = ""
    json_data: Any = None
    url: str = ""

    def __post_init__(self) -> None:
        if not self.url:
            self.url = ""

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def json(self) -> Any:
        if self.json_data is None:
            raise ValueError("FakeResponse has no json_data configured")
        return self.json_data


class FakeSession:
    """Scripted stand-in for `requests.Session`.

    `add(url, response)` registers one response for one URL (exact
    match). Calling the same URL more than once replays the SAME
    response unless multiple were queued via repeated `add()` calls (a
    FIFO per URL), which is enough to test pagination (page 1, page 2,
    ...) and retry sequences (fail once, then succeed).

    `response` may be a `FakeResponse`, an `Exception` instance (raised
    instead of returned — simulates a network error), or a zero-arg
    callable returning either.

    Unregistered URLs default to a plain 404 `FakeResponse`, not an
    error — this matters for enumeration-style walkers (id_enum,
    daily_star_nodes) that probe MANY candidate URLs where only a few
    are meant to "exist"; a real unregistered ID naturally 404s rather
    than crashing the process. Pass `strict=True` to instead raise
    `AssertionError` on any unregistered URL, for tests that want to
    pin down exactly which URLs a code path is expected to call.
    """

    def __init__(self, *, strict: bool = False) -> None:
        self._by_url: dict[str, list[Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.strict = strict

    def add(self, url: str, response: Any) -> FakeSession:
        self._by_url.setdefault(url, []).append(response)
        return self

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url))
        bucket = self._by_url.get(url)
        if not bucket:
            if self.strict:
                raise AssertionError(
                    f"FakeSession: no response registered for {method} {url}. Registered: {list(self._by_url)}"
                )
            return FakeResponse(status_code=404, url=url)
        item = bucket.pop(0) if len(bucket) > 1 else bucket[0]
        if callable(item) and not isinstance(item, FakeResponse):
            item = item()
        if isinstance(item, Exception):
            raise item
        if not item.url:
            item.url = url
        return item

    @property
    def call_count(self) -> int:
        return len(self.calls)


@dataclass
class ScriptedResolver:
    """Helper for id_enum/daily_star-style tests: maps a URL to a
    (status_code, html) pair, used by a monkeypatched `app.sources.http.get`.
    """

    pages: dict[str, FakeResponse] = field(default_factory=dict)

    def get(self, url: str) -> FakeResponse | None:
        return self.pages.get(url)

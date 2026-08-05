"""Outbound-socket guard — makes mission §7 ("NO RE-FETCHING TO TEST
DOWNSTREAM ... a P2 or P3 test that tries to open a socket should fail
loudly") structural rather than aspirational.

Wired in as a session-wide autouse fixture by `tests/conftest.py`; this
module holds the actual monkeypatch so it's independently importable/
testable (see `tests/test_harness_network_guard.py`) without depending on
pytest fixture machinery.

Blocks at the lowest common layer both `requests` and any raw-socket code
go through: `socket.socket.connect`/`connect_ex`. This does NOT block:
  * Starlette's `TestClient` (`tests/conftest.py`'s `client` fixture) —
    it drives the ASGI app in-process over an in-memory transport, no
    real socket involved, so `/healthz` etc. are unaffected.
  * `sqlite3` file-based connections (`data/news.db`, when a test
    legitimately needs read-only DB access) — not a socket at all.
  * DNS-free loopback used internally by the test process itself.

Escape hatch: `@pytest.mark.allow_network` on a specific test. No test in
this repo uses it today — it exists for a FUTURE on-demand harness (e.g.
someone actually running the parity harness against a live provider,
which this task explicitly does not do) to opt in deliberately and
visibly, rather than the guard being quietly worked around.
"""

from __future__ import annotations

import socket
from typing import Any


class NetworkBlockedError(RuntimeError):
    """Raised in place of an actual outbound connection attempt."""


def _blocked(method_name: str):
    def _raiser(self: socket.socket, address: Any) -> Any:
        raise NetworkBlockedError(
            f"Outbound network call blocked by the test-suite network guard "
            f"(socket.socket.{method_name}({address!r})). Mission §7: tests must "
            "not re-fetch over the network to exercise a downstream stage. If "
            "this specific test genuinely needs network, mark it "
            "`@pytest.mark.allow_network` — and explain why in the test docstring."
        )

    return _raiser


_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex


def block_network() -> None:
    """Monkeypatch socket.socket.connect/connect_ex process-wide to raise
    NetworkBlockedError instead of opening a real connection. Idempotent —
    safe to call when already blocked.
    """
    socket.socket.connect = _blocked("connect")  # type: ignore[method-assign]
    socket.socket.connect_ex = _blocked("connect_ex")  # type: ignore[method-assign]


def restore_network() -> None:
    """Undo block_network(), restoring the real stdlib implementations."""
    socket.socket.connect = _ORIGINAL_CONNECT  # type: ignore[method-assign]
    socket.socket.connect_ex = _ORIGINAL_CONNECT_EX  # type: ignore[method-assign]

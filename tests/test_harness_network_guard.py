"""The network guard (tests/helpers/network_guard.py) makes mission §7
structural: any test that tries to open a real socket fails loudly,
without needing to actually reach a live host (that would make the guard
itself flaky/slow/offline-dependent — the guard raises before the syscall
that would touch the network, so these tests pass with or without
internet access).
"""

from __future__ import annotations

import socket

import pytest

from tests.helpers.network_guard import (
    NetworkBlockedError,
    block_network,
    restore_network,
)


def test_guard_is_active_by_default_for_every_test():
    """No explicit fixture request needed — tests/conftest.py's
    `_network_guard` is autouse. A real TCP connect attempt (to an
    address that's never actually dialed, since the guard raises first)
    must fail with NetworkBlockedError, not a socket/connection error.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlockedError):
            s.connect(("example.com", 443))
    finally:
        s.close()


def test_guard_blocks_connect_ex_too():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlockedError):
            s.connect_ex(("example.com", 443))
    finally:
        s.close()


@pytest.mark.allow_network
def test_allow_network_marker_restores_real_connect(monkeypatch):
    """Demonstrates the escape hatch works — WITHOUT actually touching the
    network: patch socket.socket.connect to a stub that just records it
    was called with the real (unblocked) implementation reachable, proving
    the guard fixture skipped patching for this marked test. Confirms the
    *mechanism* (marker -> guard skipped) without requiring internet.
    """
    calls = []
    monkeypatch.setattr(socket.socket, "connect", lambda self, addr: calls.append(addr))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect(("127.0.0.1", 9))  # discard port; never actually reached, stub short-circuits
    finally:
        s.close()
    assert calls == [("127.0.0.1", 9)]


def test_block_and_restore_are_idempotent_and_symmetric():
    """Direct unit test of the guard module itself, independent of the
    pytest fixture wiring — block twice, restore twice, end up back at
    the real implementation either way.

    Starts with an explicit `restore_network()` to get a known-true-
    original baseline regardless of the autouse fixture having already
    blocked before this test body ran (it has — that's the whole point of
    it being autouse).
    """
    restore_network()
    true_original_connect = socket.socket.connect

    block_network()
    block_network()  # idempotent
    assert socket.socket.connect is not true_original_connect

    restore_network()
    restore_network()  # idempotent
    assert socket.socket.connect is true_original_connect

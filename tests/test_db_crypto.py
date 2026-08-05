"""Provider API key encryption at rest (ADR §9.1, DoD #8): never
recoverable except via the one decrypt call, masked-suffix display only,
key sourced from an env var, and rotation actually works.
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from app.db.crypto import decrypt_api_key, encrypt_api_key, rotate_api_key


def test_encrypt_decrypt_round_trips() -> None:
    key = Fernet.generate_key().decode()
    ciphertext, masked = encrypt_api_key("gsk_super_secret_value_123456", key=key)

    assert isinstance(ciphertext, bytes)
    assert b"super_secret" not in ciphertext  # plaintext never appears verbatim in the stored bytes
    assert masked == "*" * (len("gsk_super_secret_value_123456") - 4) + "3456"

    recovered = decrypt_api_key(ciphertext, key=key)
    assert recovered == "gsk_super_secret_value_123456"


def test_masked_suffix_never_exposes_more_than_last_four() -> None:
    key = Fernet.generate_key().decode()
    _, masked = encrypt_api_key("short", key=key)
    assert masked.endswith("hort")
    assert "s" not in masked[:-4] if len(masked) > 4 else True


def test_decrypt_with_wrong_key_raises_configuration_error() -> None:
    from app.errors import ConfigurationError

    key_a = Fernet.generate_key().decode()
    key_b = Fernet.generate_key().decode()
    ciphertext, _ = encrypt_api_key("a-real-key", key=key_a)

    try:
        decrypt_api_key(ciphertext, key=key_b)
        raise AssertionError("expected ConfigurationError")
    except ConfigurationError:
        pass


def test_rotate_api_key_re_encrypts_under_new_key_and_old_key_stops_working() -> None:
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()

    ciphertext, _ = encrypt_api_key("rotate-me-please", key=old_key)
    rotated = rotate_api_key(ciphertext, old_key=old_key, new_key=new_key)

    assert decrypt_api_key(rotated, key=new_key) == "rotate-me-please"

    from app.errors import ConfigurationError

    try:
        decrypt_api_key(rotated, key=old_key)
        raise AssertionError("expected old key to no longer decrypt the rotated ciphertext")
    except ConfigurationError:
        pass


def test_rotate_is_idempotent_if_run_twice() -> None:
    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    ciphertext, _ = encrypt_api_key("value", key=old_key)

    once = rotate_api_key(ciphertext, old_key=old_key, new_key=new_key)
    # already rotated -- old_key won't match this time, new_key will (MultiFernet tries new_key first)
    twice = rotate_api_key(once, old_key=old_key, new_key=new_key)

    assert decrypt_api_key(twice, key=new_key) == "value"

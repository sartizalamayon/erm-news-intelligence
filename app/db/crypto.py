"""Encryption for admin-entered LLM provider API keys at rest (ADR
§9.1). See `migrations/0006_provider_configs.sql` for the storage shape.

The key: `app.config.Settings.secret_key` (env var `SECRET_KEY`) — read
from the environment once per call, NEVER read from or written to the
database. This module has no fallback that would let it silently run
unencrypted; a missing key is a `ConfigurationError`, not a degraded
mode.

Rotation: Fernet tokens don't embed which key encrypted them, so a
single `Fernet(key)` can't tell "wrong key" apart from "corrupt data" —
but `cryptography.fernet.MultiFernet` (same package, already a
dependency, no new install) can decrypt with any key in a list and
always encrypts with the first. `rotate_api_key` below uses it for a
one-row re-encrypt; running it over every `provider_configs` row is the
full rotation procedure:

    1. Deploy with both the current key and a new key available
       (e.g. `SECRET_KEY` = new, `SECRET_KEY_OLD` = current — adding
       that second env var is a one-line app.config.py change for
       whoever performs the first rotation; ARCH-DB does not own that
       file).
    2. For each row: `rotate_api_key(ciphertext, old_key=SECRET_KEY_OLD,
       new_key=SECRET_KEY)` and write the result back.
    3. Remove `SECRET_KEY_OLD` once every row is confirmed re-encrypted.

Never held in the same place as the ciphertext (the database) at any
point in that procedure — both keys are environment variables, both
supplied by whoever runs the rotation, never persisted.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.config import get_settings
from app.errors import ConfigurationError
from app.logging import mask


def _key_bytes(key: str) -> bytes:
    return key.encode("utf-8") if isinstance(key, str) else key


def _fernet(key: str | None = None) -> Fernet:
    resolved = key if key is not None else get_settings().secret_key
    if not resolved:
        raise ConfigurationError(
            "SECRET_KEY is not configured — cannot encrypt/decrypt provider API keys. "
            'Generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(_key_bytes(resolved))


def encrypt_api_key(plaintext: str, *, key: str | None = None) -> tuple[bytes, str]:
    """Returns (ciphertext, masked_suffix) ready for
    `provider_configs.api_key_ciphertext` / `.api_key_last4`. The
    plaintext never leaves this call — no logging, no return path other
    than the ciphertext and the masked display string.
    """
    ciphertext = _fernet(key).encrypt(plaintext.encode("utf-8"))
    return ciphertext, mask(plaintext)


def decrypt_api_key(ciphertext: bytes, *, key: str | None = None) -> str:
    """The one read path that recovers plaintext. Intended caller: the
    LLM client at request time, immediately before use — never an
    admin-facing read endpoint (ADR §9.1: "never echoed back beyond a
    masked suffix"). Raises ConfigurationError (not the raw
    cryptography exception) so callers can treat "wrong/rotated key" and
    "not configured" the same way.
    """
    try:
        return _fernet(key).decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        raise ConfigurationError(
            "stored provider API key could not be decrypted — SECRET_KEY rotated without re-encrypting?"
        ) from exc


def rotate_api_key(ciphertext: bytes, *, old_key: str, new_key: str) -> bytes:
    """Re-encrypt one row's ciphertext under `new_key`.

    `MultiFernet.rotate()` decrypts with whichever key in the list
    actually matches (tries `new_key` first, then `old_key` — so a row
    that was already rotated in a prior, interrupted run decrypts fine
    too, making the per-row rotation loop safely re-runnable) and always
    re-encrypts with the first key, `new_key`.
    """
    multi = MultiFernet([_fernet(new_key), _fernet(old_key)])
    try:
        return multi.rotate(ciphertext)
    except InvalidToken as exc:
        raise ConfigurationError("could not decrypt with either the old or the new key during rotation") from exc

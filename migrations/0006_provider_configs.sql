-- 0006_provider_configs.sql
--
-- Admin-entered LLM provider API keys, encrypted at rest (ADR §9.1: "API
-- keys enterable from the admin dashboard... provider API keys entered
-- through a web form and stored in the database must be encrypted at
-- rest, write-only from the client's perspective (never echoed back
-- beyond a masked suffix), admin-only enforced server-side, and
-- excluded from all logging").
--
-- What is stored: `api_key_ciphertext` is a Fernet token (see
-- app/db/crypto.py) — opaque bytes, not the plaintext key. There is no
-- read path anywhere in this schema or in app/db/crypto.py that returns
-- the plaintext key to a caller other than the one decrypt call the LLM
-- client itself makes at request time; every other consumer (the admin
-- UI listing configured providers) is expected to read `api_key_last4`
-- only. The encryption key is `app.config.Settings.secret_key` — an
-- environment variable (SECRET_KEY), NEVER a value stored in this or
-- any other table. This migration does not and must not seed a key.
--
-- Rotation: see app/db/crypto.py module docstring — Fernet tokens are
-- self-describing (they embed which key encrypted them isn't stored,
-- but `cryptography.fernet.MultiFernet` can decrypt with an old key and
-- re-encrypt with a new one), so rotating SECRET_KEY is:
--   1. deploy with SECRET_KEY_NEXT (a second env var) added alongside
--      the current SECRET_KEY,
--   2. run a one-off re-encrypt pass (MultiFernet([new, old]).rotate()
--      on every row),
--   3. promote SECRET_KEY_NEXT to SECRET_KEY and drop the old value.
-- `app.config.Settings` only defines `secret_key` today; adding
-- `secret_key_next` is a one-line config change for whoever performs
-- the first rotation, not built here since ARCH-DB does not own
-- app/config.py.

CREATE TABLE provider_configs (
    id                  BIGSERIAL PRIMARY KEY,
    provider            TEXT NOT NULL UNIQUE CHECK (provider IN ('groq', 'gemini')),
    api_key_ciphertext  BYTEA,                          -- NULL = not configured; the bootstrap env-var key (app.config.Settings) is used instead.
    api_key_last4       TEXT NOT NULL DEFAULT '',        -- masked-suffix display only (app.logging.mask()); never the plaintext key.
    model               TEXT NOT NULL DEFAULT '',
    role                TEXT NOT NULL DEFAULT 'secondary' CHECK (role IN ('primary', 'secondary', 'disabled')),
    updated_by          BIGINT REFERENCES users (id) ON DELETE SET NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- `UNIQUE (provider)` is the only lookup: at most one row per provider
-- ('groq', 'gemini' — ADR §9.1's ruled set), read in full at LLM-client
-- construction time. Table is bounded to ~2 rows; no other index
-- justified.

INSERT INTO provider_configs (provider, model, role) VALUES
    ('groq', 'qwen/qwen3.6-27b', 'primary'),
    ('gemini', 'gemini-2.0-flash', 'secondary')
ON CONFLICT (provider) DO NOTHING;

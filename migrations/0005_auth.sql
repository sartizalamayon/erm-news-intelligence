-- 0005_auth.sql
--
-- users / sessions / otp_codes — replaces legacy's single-row
-- `username`/`password_hash` table (legacy-behavior.md §6: "users — 1
-- row, role admin") with the OTP-only model ADR §9.3 ruled: "The
-- owner's suggested transport does not work... OTP: short expiry,
-- single use, rate-limited per address and per IP." There is no
-- password anywhere in this schema on purpose — `password_hash` is
-- DROPPED, not carried forward, because ADR §9.3 rules out password
-- auth entirely in favor of email OTP. `username` is DROPPED too,
-- replaced by `email` (frontend-contract.md §3.0: work-email sign-in,
-- restricted to one domain — see `settings.allowed_email_domain`,
-- 0001_settings.sql).
--
-- Neither table stores a secret in recoverable form: `otp_codes.code_hash`
-- and `sessions.token_hash` are one-way hashes (the actual OTP digits /
-- session token exist only in the email sent to the user / the client's
-- cookie, never at rest here) — same "never retrievable, only provable"
-- posture as the provider-key encryption in 0006.

CREATE TABLE users (
    id             BIGSERIAL PRIMARY KEY,
    email          TEXT NOT NULL UNIQUE,   -- app layer must lower() before insert/lookup; no citext extension pulled in for one column.
    role           TEXT NOT NULL DEFAULT 'analyst' CHECK (role IN ('admin', 'analyst')),
    display_name   TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at  TIMESTAMPTZ
);
-- `UNIQUE (email)` is the only lookup this table needs — login resolves
-- a user by email, everything else joins by `users.id`.

CREATE TABLE otp_codes (
    id             BIGSERIAL PRIMARY KEY,
    email          TEXT NOT NULL,
    code_hash      TEXT NOT NULL,
    purpose        TEXT NOT NULL DEFAULT 'login',
    expires_at     TIMESTAMPTZ NOT NULL,
    consumed_at    TIMESTAMPTZ,
    attempt_count  INTEGER NOT NULL DEFAULT 0,
    requested_ip   TEXT NOT NULL DEFAULT '',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Intended query: per-address rate limiting (ADR §9.3 / brief §10,
-- "rate-limited per address and per IP") —
--   SELECT count(*) FROM otp_codes WHERE email = ? AND created_at > now() - interval '1 hour';
CREATE INDEX idx_otp_codes_email_created ON otp_codes (email, created_at);
-- Intended query: the IP half of the same rate limit —
--   SELECT count(*) FROM otp_codes WHERE requested_ip = ? AND created_at > now() - interval '1 hour';
CREATE INDEX idx_otp_codes_ip_created ON otp_codes (requested_ip, created_at);
-- Intended query: verifying a submitted code against the single
-- still-live one for that address —
--   SELECT * FROM otp_codes WHERE email = ? AND consumed_at IS NULL AND expires_at > now();
CREATE INDEX idx_otp_codes_email_active ON otp_codes (email) WHERE consumed_at IS NULL;

CREATE TABLE sessions (
    id           BIGSERIAL PRIMARY KEY,
    user_id      BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    user_agent   TEXT NOT NULL DEFAULT '',
    ip_address   TEXT NOT NULL DEFAULT ''
);
-- Intended query: validating a bearer/cookie token on every
-- authenticated request —
--   SELECT * FROM sessions WHERE token_hash = ? AND revoked_at IS NULL AND expires_at > now();
CREATE UNIQUE INDEX idx_sessions_token_hash ON sessions (token_hash);
-- Intended query: "log out everywhere" / listing a user's active
-- sessions —
--   SELECT * FROM sessions WHERE user_id = ? AND revoked_at IS NULL;
CREATE INDEX idx_sessions_user_id ON sessions (user_id);

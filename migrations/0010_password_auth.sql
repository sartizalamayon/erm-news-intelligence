-- 0010_password_auth.sql
--
-- Owner-ruled auth model change (supersedes ADR §9.3's "OTP only"):
-- OTP is now used ONCE per account (signup, and password reset) to
-- prove mailbox ownership; every subsequent sign-in is email+password.
-- This trades the per-login email requirement (which needed a verified
-- sending domain the owner has ruled out getting DNS access for) for a
-- one-time-per-account email, at the cost of needing real password
-- storage, which 0005_auth.sql deliberately did not have.
--
-- `password_hash` is a PHC-encoded Argon2id string (self-describing:
-- algorithm, version, cost parameters, salt, and hash all in one text
-- value -- see app/services/auth.py's hash_password()/verify_password()).
-- Empty string ('') means "no password set yet" and is the single
-- source of truth for whether an account can sign in with a password
-- yet -- both a brand-new self-provisioned account and the existing
-- seeded bootstrap admin (migrations/0009_seed_admin.sql,
-- nt.sartiz@grameenphone.com) start in this state and go through the
-- same OTP "set password" flow to leave it; no separate bootstrap
-- mechanism needed.
ALTER TABLE users
    ADD COLUMN password_hash TEXT NOT NULL DEFAULT '';

-- Login is now a guessable-secret check (it never was one before --
-- there was no password), so it needs the same shape of per-email/
-- per-IP rate limiting otp_codes already has, over a table of its own:
-- login attempts aren't OTP requests and don't belong in that table.
CREATE TABLE login_attempts (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL,
    ip_address    TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Intended query (mirrors idx_otp_codes_email_created's shape exactly):
--   SELECT count(*) FROM login_attempts WHERE email = ? AND created_at > now() - interval '15 minutes';
CREATE INDEX idx_login_attempts_email_created ON login_attempts (email, created_at);
-- Intended query -- the IP half of the same rate limit:
--   SELECT count(*) FROM login_attempts WHERE ip_address = ? AND created_at > now() - interval '15 minutes';
CREATE INDEX idx_login_attempts_ip_created ON login_attempts (ip_address, created_at);
